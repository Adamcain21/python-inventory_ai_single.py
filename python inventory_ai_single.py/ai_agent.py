"""
Inventory AI — real AI assistant (Claude + tool use)
=====================================================
Understands free-form requests in any language, with typos
("give me 3 bag of carrot 5 steaks and 3 oil olive and some crush can"),
and acts through TOOLS that call the existing business logic in
inventory_ai.py. The safety nets (weekly budget, capacity cap, duplicate
cooldown, case rounding, cheapest supplier) live there, so the AI cannot
bypass them. It can also add new ingredients and suppliers, because every
restaurant orders different things.

Needs an Anthropic API key:   set ANTHROPIC_API_KEY=sk-ant-...
Without a key, /chat falls back to the simple English pattern matcher.
Optional: INVENTORY_AI_MODEL (default claude-opus-5).
"""

import io
import json
import os
from contextlib import contextmanager, redirect_stdout
from datetime import datetime
from typing import Callable, Dict, List, Optional

import db
import inventory_ai as ai
from regional_catalog import PRODUCTS, exact_product, find_products

CATALOG_UNITS = {name: spec[0] for name, spec in PRODUCTS.items()}
MODEL = os.environ.get("INVENTORY_AI_MODEL", "claude-opus-5")
EFFORT = os.environ.get("INVENTORY_AI_EFFORT", "low")   # low = fastest answers, medium/high = more careful
FALLBACK_BETA = "server-side-fallback-2026-07-01"   # re-runs a (rare) safety refusal on another model
MAX_TOOL_ROUNDS = 12
MAX_HISTORY_MESSAGES = 20
TRIGGER = "ai-chat"


def is_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def make_client():
    import anthropic
    return anthropic.Anthropic()


@contextmanager
def _capture():
    buf = io.StringIO()
    with redirect_stdout(buf):
        yield buf


# ═══════════════════════════════════════════════════════════════
# 1. PROMPT + TOOL DEFINITIONS
# ═══════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """\
You are the AI assistant of the restaurant kitchen "{location}" (region: {region}; today is {today}). \
Talk like a friendly, \
knowledgeable chef colleague: natural conversation, warm and to the point, in the \
language the staff write in (they are often in a hurry, with typos, in English or French).

What you help with - anything food and restaurant related:
- this kitchen's stock, orders, suppliers and sales, through your tools;
- cooking questions: recipes, techniques, substitutions, portions, scaling a recipe;
- food safety, storage and shelf life, allergens;
- menu ideas, using up what is in stock before it spoils, food cost and pricing.
For topics unrelated to food or running a restaurant, say kindly in one sentence that \
you're the kitchen's food assistant, and bring the conversation back.

Answer general questions from your own knowledge; when the question is about this \
kitchen ("what can I cook with what we have?", "are we low on anything?"), check the \
real data with get_inventory first rather than guessing.

When the staff ask for stock or orders:
- Start with get_inventory to see what this restaurant stocks, the unit each item is \
counted in, and which items have suppliers.
- Match what the staff wrote to existing ingredients when it is clearly the same thing \
("oil olive" -> an existing olive oil item). If two items could match, ask which one.
- Tool quantities are always in the ingredient's own unit. If the staff used another \
unit (bag, case, box, tub, can) and you don't know the conversion, ask one short \
question (e.g. "How many kg in a bag?") instead of guessing.
- When a quantity could mean different things and the order is expensive ("5 steaks": \
5 pieces or 5 kg?), ask before ordering.
- Catalog prices are per the catalog unit (compare_prices shows it). If an ingredient \
is counted in another unit (e.g. mayonnaise in "tub" but priced per L), ask for the \
conversion ("How many L in your tub?") and fix it with update_ingredient before ordering. \
If an ingredient's name differs from the catalog product (e.g. "mayo" vs "mayonnaise"), \
rename it with update_ingredient so its suppliers apply.
- Restaurants order different things: when an item isn't in the inventory, add it with \
add_ingredient without asking permission. Call compare_prices first: if the regional \
catalog already sells it, reuse the catalog's product name and unit so its suppliers \
apply. Otherwise pick a clear lowercase name ("steak", "crushed_tomatoes_can"), a \
sensible unit and aliases for the words the staff use. If the item itself is unclear \
("crush can"), ask what it is first.
- Only suppliers of this restaurant's region are used. Many catalog prices are \
ESTIMATES (distributors don't publish prices): whenever you quote or order at an \
estimated price, say it is estimated, and mention a confirmed price older than 30 \
days (price_date). Use compare_prices when the staff ask who is cheapest. Real prices \
come from invoices: suggest the "Prix → Importer une facture" screen when prices are \
missing or old.
- Never invent suppliers or prices. If an item has no supplier in the region, ask for \
the supplier name, price per unit and delivery time, then call add_supplier and place \
the order. A price the staff give you is a confirmed price.
- Order like a smart buyer: when the staff ask for several items, call order_items ONCE \
with all of them (never place_order item by item), so they are grouped into one delivery \
per supplier, the delivery fee is paid once and each supplier's minimum order is respected. \
If a delivery is below its supplier's minimum, don't split it into small orders: tell how \
much is missing and propose items to add (suggest_order lists items getting low) or to \
wait. Mention the number of deliveries and the total with fees. Items ordered later the \
same day automatically join that supplier's delivery already ordered today when it is \
the best choice (no second delivery fee): say so when it happens (joins_open_delivery).
- Orders can be blocked by the weekly budget, the duplicate-order cooldown or a supplier \
minimum: report the block. Use force / allow_below_minimum only if the staff explicitly insist.
- Supplier minimums and delivery fees are estimates until the staff give the real ones: \
save them with set_supplier_terms.
- Handle every item in the message. Order what you can now, and ask about the rest in \
the same reply.

Style: conversational, not robotic. Keep answers short enough to read during service. \
When you place or block orders, end with one easy-to-scan line per item \
(✅ ordered / ⚠️ blocked / ❓ question) with quantity, supplier and cost."""


def _obj(properties: dict, required: List[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOLS = [
    {
        "name": "get_inventory",
        "description": "Current inventory of this restaurant: every ingredient with its unit, stock, "
                       "reorder point, the words staff use for it (aliases), how many suppliers of the "
                       "region sell it and the cheapest one. Also the weekly budget and the dishes.",
        "input_schema": _obj({}, []),
    },
    {
        "name": "compare_prices",
        "description": "Compare every supplier of this restaurant's region for one product (also products "
                       "not stocked yet, from the regional catalog): real total cost for the quantity "
                       "(case rounding + delivery fee), delivery days, and whether the price is estimated.",
        "input_schema": _obj({
            "product": {"type": "string", "description": "Product name or alias, e.g. 'salmon', 'olive oil'"},
            "quantity": {"type": "number", "exclusiveMinimum": 0, "description": "Default 1"},
        }, ["product"]),
    },
    {
        "name": "place_order",
        "description": "Order an ingredient. The cheapest supplier (real total cost incl. delivery) is chosen "
                       "automatically and the quantity is rounded up to whole cases if the supplier sells by the "
                       "case. Blocked if it would exceed the weekly budget or if the same ingredient was ordered "
                       "in the last few hours (cooldown).",
        "input_schema": _obj({
            "ingredient": {"type": "string", "description": "Ingredient name or alias, as in get_inventory"},
            "quantity": {"type": "number", "description": "Quantity in the ingredient's unit (must be > 0)"},
            "force": {"type": "boolean", "description": "Skip the duplicate-order cooldown. Only when the staff "
                                                        "explicitly asks to order again anyway."},
        }, ["ingredient", "quantity"]),
    },
    {
        "name": "order_items",
        "description": "Order SEVERAL ingredients at once (use this whenever there is more than one item). "
                       "Items are grouped into one delivery per supplier: the delivery fee is paid once, each "
                       "supplier's minimum order is respected, and the cheapest real total wins (fewer "
                       "deliveries preferred when the cost is about the same). Returns what was ordered per "
                       "delivery and what was not (below a minimum, over budget, cooldown) with the reason.",
        "input_schema": _obj({
            "items": {"type": "array", "items": _obj({
                "ingredient": {"type": "string"},
                "quantity": {"type": "number", "exclusiveMinimum": 0,
                             "description": "In the ingredient's unit. Omit = its usual reorder quantity"},
            }, ["ingredient"])},
            "force": {"type": "boolean", "description": "Skip the duplicate cooldown (only if staff insists)"},
            "allow_below_minimum": {"type": "boolean",
                                    "description": "Order even below a supplier's minimum (only if staff insists)"},
        }, ["items"]),
    },
    {
        "name": "preview_order",
        "description": "Same planning as order_items WITHOUT ordering: deliveries per supplier, fees, and "
                       "whether each delivery reaches its supplier's minimum. Use it to answer 'how much would it cost'.",
        "input_schema": _obj({"items": {"type": "array", "items": _obj({
            "ingredient": {"type": "string"}, "quantity": {"type": "number", "exclusiveMinimum": 0},
        }, ["ingredient"])}}, ["items"]),
    },
    {
        "name": "suggest_order",
        "description": "What should be ordered now: items below their reorder point with nothing on the way, "
                       "planned per supplier, plus items getting low that could be added to reach a minimum.",
        "input_schema": _obj({}, []),
    },
    {
        "name": "set_supplier_terms",
        "description": "Save a supplier's real delivery conditions given by the staff (e.g. 'Sysco: 250$ minimum, "
                       "free delivery'): minimum order value, fee per delivery, free delivery threshold.",
        "input_schema": _obj({
            "supplier": {"type": "string"},
            "min_order_value": {"type": "number", "minimum": 0},
            "delivery_fee": {"type": "number", "minimum": 0},
            "free_delivery_over": {"type": "number", "minimum": 0},
        }, ["supplier", "min_order_value"]),
    },
    {
        "name": "order_for_people",
        "description": "For 'I need X for N people': checks the capacity safety cap, computes the need from "
                       "the recipes and orders only the shortfall.",
        "input_schema": _obj({
            "ingredient": {"type": "string"},
            "people": {"type": "integer", "minimum": 1},
        }, ["ingredient", "people"]),
    },
    {
        "name": "add_ingredient",
        "description": "Add a NEW ingredient to this restaurant's inventory (fails if it already exists). "
                       "Stock starts at 0 unless given. reorder_point 0 means it is never auto-reordered.",
        "input_schema": _obj({
            "name": {"type": "string", "description": "Short lowercase name, e.g. 'steak', 'olive_oil'"},
            "unit": {"type": "string", "description": "How it is counted: unit, kg, L, can, bottle, case..."},
            "aliases": {"type": "string", "description": "Comma-separated words staff use, e.g. 'steak,steaks'"},
            "stock": {"type": "number", "minimum": 0},
            "reorder_point": {"type": "number", "minimum": 0},
            "reorder_qty": {"type": "number", "exclusiveMinimum": 0},
            "shelf_life_days": {"type": "number", "exclusiveMinimum": 0},
        }, ["name", "unit", "aliases"]),
    },
    {
        "name": "update_ingredient",
        "description": "Fix an existing ingredient: rename it (e.g. to the catalog product name so its "
                       "suppliers apply) and/or change its unit. When the unit changes, unit_factor = how "
                       "many NEW units are in one OLD unit (tub -> L with a 4 L tub: 4); stock, thresholds and "
                       "recipe quantities are converted with it.",
        "input_schema": _obj({
            "name": {"type": "string", "description": "Current ingredient name"},
            "new_name": {"type": "string"},
            "unit": {"type": "string", "description": "New unit"},
            "unit_factor": {"type": "number", "exclusiveMinimum": 0},
            "aliases": {"type": "string", "description": "Replaces the aliases (comma-separated)"},
        }, ["name"]),
    },
    {
        "name": "add_supplier",
        "description": "Add a supplier offer for an ingredient. Only with details the staff gave you - "
                       "never invent a supplier or a price.",
        "input_schema": _obj({
            "name": {"type": "string", "description": "Supplier name, e.g. 'Metro'"},
            "ingredient": {"type": "string"},
            "price_per_unit": {"type": "number", "exclusiveMinimum": 0,
                               "description": "Price for ONE unit of the ingredient's unit"},
            "delivery_days": {"type": "number", "minimum": 0},
            "delivery_fee": {"type": "number", "minimum": 0},
            "case_size": {"type": "number", "exclusiveMinimum": 0,
                          "description": "Only if the supplier sells by the case/pack only"},
            "free_shipping_at": {"type": "number", "exclusiveMinimum": 0},
            "reliability_score": {"type": "number", "minimum": 0, "maximum": 1,
                                  "description": "0-1, default 0.9 if unknown"},
        }, ["name", "ingredient", "price_per_unit", "delivery_days"]),
    },
    {
        "name": "punch_sale",
        "description": "Record dishes sold: deducts the recipe's ingredients from stock and auto-orders "
                       "anything that falls below its reorder point.",
        "input_schema": _obj({
            "dish": {"type": "string", "description": "Dish name or alias"},
            "servings": {"type": "integer", "minimum": 1},
        }, ["dish", "servings"]),
    },
    {
        "name": "list_orders",
        "description": "Order history of this restaurant, newest first.",
        "input_schema": _obj({
            "status": {"type": "string", "enum": ["pending", "received", "cancelled"]},
        }, []),
    },
    {
        "name": "receive_order",
        "description": "Mark a pending order as delivered: adds its quantity to stock.",
        "input_schema": _obj({"order_id": {"type": "string", "description": "e.g. ORD-00012"}}, ["order_id"]),
    },
    {
        "name": "cancel_order",
        "description": "Cancel a pending order and refund it from the weekly spend.",
        "input_schema": _obj({"order_id": {"type": "string"}}, ["order_id"]),
    },
]


# ═══════════════════════════════════════════════════════════════
# 2. TOOL IMPLEMENTATIONS (thin wrappers over inventory_ai / db)
# ═══════════════════════════════════════════════════════════════

class ToolError(Exception):
    pass


def _order_summary(o: ai.Order) -> dict:
    return {"order_id": o.order_id, "ingredient": o.ingredient, "quantity": o.quantity,
            "supplier": o.supplier.name, "total_cost": o.total_cost, "status": o.status,
            "expected_arrival": f"{o.expected_arrival:%Y-%m-%d}", "trigger": o.trigger, "po_id": o.po_id}


class ToolBox:
    """Runs tools for one location. `guard` wraps every tool call (the API passes
    its lock + stdout capture, so tools never race with other requests)."""

    def __init__(self, location_key: str, guard: Callable = _capture):
        self.key = location_key
        self.guard = guard
        self.log: List[str] = []

    @property
    def loc(self) -> ai.Location:
        return ai.LOCATIONS[self.key]   # looked up each time: config edits reload the objects

    def run(self, name: str, args: dict):
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return f"Error: unknown tool '{name}'.", True
        with self.guard() as buf:
            try:
                result, is_error = fn(**(args or {})), False
            except (ToolError, db.ConfigError, ValueError, TypeError, KeyError) as e:
                result, is_error = f"Error: {e}", True
        if buf is not None:
            self.log += [line for line in buf.getvalue().splitlines() if line.strip()]
        return (result if isinstance(result, str) else json.dumps(result, default=str)), is_error

    # ---------- helpers ----------

    def _ingredient(self, raw: str) -> str:
        name = ai._normalize_item(raw) or db.norm_name(raw)
        if self.loc.inventory.get(name) is None:
            hint = f" It is the catalog product '{name}': add_ingredient(name='{name}')." if name in CATALOG_UNITS else ""
            raise ToolError(f"'{raw}' is not in {self.loc.name}'s inventory. Use add_ingredient first.{hint}")
        return name

    def _own_order(self, order_id: str) -> ai.Order:
        order_id = order_id.strip().upper()
        order = next((o for o in self.loc.order_history if o.order_id == order_id), None)
        if order is None:
            raise ToolError(f"No order {order_id} at {self.loc.name}.")
        return order

    # ---------- tools ----------

    def tool_get_inventory(self):
        loc = self.loc
        aliases: Dict[str, List[str]] = {}
        for word, name in ai.INGREDIENT_ALIASES.items():
            aliases.setdefault(name, []).append(word)
        def cheapest(name):
            offers = ai.suppliers_for(name, loc.region)
            if not offers:
                return None
            s = min(offers, key=lambda o: o.price_per_unit)
            return {"supplier": s.name, "price_per_unit": s.price_per_unit, "estimated": s.price_source == "estimate"}

        def entry(name, i):
            e = {"name": name, "unit": i.unit, "stock": round(i.stock, 3), "reorder_point": i.reorder_point,
                 "reorder_qty": i.reorder_qty, "aliases": aliases.get(name, []),
                 "suppliers_in_region": len(ai.suppliers_for(name, loc.region)), "cheapest": cheapest(name)}
            if name in CATALOG_UNITS and CATALOG_UNITS[name] != i.unit:
                e["unit_problem"] = f"catalog prices are per {CATALOG_UNITS[name]}, this item is counted in {i.unit}"
            if not e["suppliers_in_region"]:
                e["catalog_matches"] = find_products(name, limit=3)
            return e

        return {
            "restaurant": loc.name,
            "region": loc.region,
            "weekly_budget": loc.weekly_budget,
            "spent_this_week": round(loc.spend_this_week, 2),
            "ingredients": [entry(name, i) for name, i in sorted(loc.inventory.items.items())],
            "dishes": ai.list_dishes(),
        }

    def tool_compare_prices(self, product: str, quantity: float = 1):
        region = self.loc.region
        name = ai._normalize_item(product) or db.norm_name(product)
        matched_from = None
        if not ai.suppliers_for(name, region):
            # 'mayo' -> mayonnaise, 'Coca-Cola' -> coke, 'frites' -> french_fries...
            candidates = [p for p in find_products(product) if ai.suppliers_for(p, region)]
            if not candidates:
                return {"product": name, "offers": [], "note": f"No supplier sells '{product}' in {region}. "
                        "Ask the staff who supplies it and at what price."}
            matched_from, name = product, candidates[0]
        offers = ai.suppliers_for(name, region)
        rows = sorted(({"supplier": s.name, "price_per_unit": s.price_per_unit,
                        "total_cost": round(s.total_cost(float(quantity)), 2),
                        "ordered_quantity": s.round_to_case(float(quantity)), "case_size": s.case_size,
                        "delivery_days": s.delivery_days, "estimated": s.price_source == "estimate",
                        "price_date": f"{s.price_updated_at:%Y-%m-%d}" if s.price_updated_at else None}
                       for s in offers), key=lambda r: r["total_cost"])
        item = self.loc.inventory.get(name)
        result = {"product": name, "in_inventory": item is not None, "catalog_unit": CATALOG_UNITS.get(name),
                  "inventory_unit": item.unit if item else None,
                  "prices_are_per": CATALOG_UNITS.get(name) or (item.unit if item else None),
                  "offers": rows[:10], "suppliers_total": len(rows)}
        if matched_from:
            result["note"] = f"'{matched_from}' matched the catalog product '{name}'."
            others = [p for p in find_products(matched_from) if p != name]
            if others:
                result["other_possible_products"] = others
        return result

    def tool_update_ingredient(self, name: str, new_name: Optional[str] = None, unit: Optional[str] = None,
                               unit_factor: Optional[float] = None, aliases: Optional[str] = None):
        row = db.update_ingredient(ai.DB_SESSION, self.loc.db_id, self._ingredient(name), new_name=new_name,
                                   unit=unit, unit_factor=unit_factor, aliases=aliases)
        ai.reload_from_db()
        item = self.loc.inventory.get(row.name)
        return {"status": "updated", "name": row.name, "unit": row.unit, "stock": round(item.stock, 3),
                "suppliers_in_region": len(ai.suppliers_for(row.name, self.loc.region))}

    def _orderable(self, ingredient: str) -> str:
        """Ingredient name if it can be ordered (has regional suppliers, units match the prices)."""
        name = self._ingredient(ingredient)
        offers = ai.suppliers_for(name, self.loc.region)
        if not offers:
            matches = [m for m in find_products(name, limit=3) if m != name and ai.suppliers_for(m, self.loc.region)]
            hint = f" The catalog has {matches}: rename it with update_ingredient." if matches else \
                " Ask the staff for supplier, price and delivery time."
            raise ToolError(f"No supplier for '{name}' in region {self.loc.region}.{hint}")
        unit = self.loc.inventory.get(name).unit
        if CATALOG_UNITS.get(name, unit) != unit and all(s.price_source == "estimate" for s in offers):
            raise ToolError(f"'{name}' is counted in '{unit}' but catalog prices are per "
                            f"'{CATALOG_UNITS[name]}'. Ask how many {CATALOG_UNITS[name]} are in one {unit}, "
                            f"then call update_ingredient(unit='{CATALOG_UNITS[name]}', unit_factor=...).")
        return name

    def _items(self, items: List[dict]) -> Dict[str, float]:
        wanted: Dict[str, float] = {}
        for it in items:
            name = self._orderable(it["ingredient"])
            qty = float(it.get("quantity") or self.loc.inventory.get(name).reorder_qty)
            wanted[name] = wanted.get(name, 0.0) + qty
        return wanted

    def tool_place_order(self, ingredient: str, quantity: float, force: bool = False):
        name = self._orderable(ingredient)
        try:
            order = self.loc.place_order(name, float(quantity), trigger=TRIGGER, force=bool(force))
        except (ai.BudgetExceededError, ai.DuplicateOrderError, ai.MinimumOrderError) as e:
            return {"result": "blocked", "reason": str(e)}
        return {"result": "ordered", "unit": self.loc.inventory.get(name).unit, **_order_summary(order)}

    def tool_preview_order(self, items: List[dict]):
        return ai.plan_purchase(self._items(items), self.loc.region,
                                open_deliveries=self.loc.open_delivery_values()).to_dict()

    def tool_order_items(self, items: List[dict], force: bool = False, allow_below_minimum: bool = False):
        result = self.loc.place_basket(self._items(items), trigger=TRIGGER, force=bool(force),
                                       allow_below_minimum=bool(allow_below_minimum), partial=True)
        deliveries: Dict[str, dict] = {}
        for o in result.orders:
            d = deliveries.setdefault(o.po_id or o.order_id, {"supplier": o.supplier.name, "lines": [], "total": 0.0})
            d["lines"].append(f"{o.quantity:g} {self.loc.inventory.get(o.ingredient).unit} {o.ingredient}")
            d["total"] = round(d["total"] + o.total_cost, 2)
        new_lines = {o.order_id for o in result.orders}
        for po in result.purchase_orders:
            d = deliveries[po.po_id]
            earlier = [o for o in self.loc.order_history if o.po_id == po.po_id and o.order_id not in new_lines]
            if earlier:
                d["added_to_todays_delivery"] = True
                d["no_extra_delivery_fee"] = True
            else:
                d["total"] = round(d["total"] + po.delivery_fee, 2)
                d["delivery_fee"] = po.delivery_fee
        return {"ordered_deliveries": deliveries, "total_cost": result.total_cost,
                "not_ordered": [{"ingredient": i, "reason": r} for i, r in result.skipped],
                "estimated_prices": any(o.supplier.price_source == "estimate" for o in result.orders)}

    def tool_suggest_order(self):
        needed, could_add = self.loc.suggested_order()
        plan = ai.plan_purchase(needed, self.loc.region, open_deliveries=self.loc.open_delivery_values()) \
            if needed else ai.PurchasePlan([], [])
        return {"needed": needed, "plan": plan.to_dict(), "could_add_to_reach_minimums": could_add,
                "open_deliveries_today": self.loc.open_delivery_values()}

    def tool_set_supplier_terms(self, supplier: str, min_order_value: float, delivery_fee: float = 0.0,
                                free_delivery_over: Optional[float] = None):
        row = db.upsert_supplier_terms(ai.DB_SESSION, supplier, self.loc.region, float(min_order_value),
                                       float(delivery_fee), free_delivery_over)
        ai.reload_from_db()
        return {"status": "saved", "supplier": row.name, "region": row.region_code,
                "min_order_value": row.min_order_value, "delivery_fee": row.delivery_fee,
                "free_delivery_over": row.free_delivery_over}

    def tool_order_for_people(self, ingredient: str, people: int):
        return ai.order_for_people(self.loc, self._ingredient(ingredient), int(people), trigger=TRIGGER)

    def tool_add_ingredient(self, name: str, unit: str, aliases: str, stock: float = 0,
                            reorder_point: float = 0, reorder_qty: float = 1,
                            shelf_life_days: Optional[float] = None):
        key = db.norm_name(name)
        note = None
        catalog_name = exact_product(name)
        if catalog_name and catalog_name != key:
            # "sauce poutine" -> gravy_mix: use the catalog product so its suppliers and prices apply
            note = f"Saved as the catalog product '{catalog_name}' (you asked '{name}')."
            aliases = ",".join(filter(None, [aliases, name.replace("_", " ")]))
            key = catalog_name
        if self.loc.inventory.get(key) is not None:
            raise ToolError(f"'{key}' already exists at {self.loc.name}.")
        catalog_unit = CATALOG_UNITS.get(key)
        if catalog_unit and unit.strip() != catalog_unit:
            # catalog prices are per catalog unit: a different unit would make every price wrong
            note = ((note + " ") if note else "") + \
                f"Unit set to '{catalog_unit}' to match the regional catalog prices (you asked '{unit}')."
            unit = catalog_unit
        db.upsert_ingredient(ai.DB_SESSION, self.loc.db_id, {
            "name": key, "unit": unit.strip() or "unit", "stock": stock, "reorder_point": reorder_point,
            "reorder_qty": reorder_qty, "min_order_qty": 1.0, "shelf_life_days": shelf_life_days,
            "daily_usage_estimate": 0.0, "aliases": aliases,
        })
        ai.reload_from_db()
        result = {"status": "added", "name": key, "unit": unit, "location": self.loc.name,
                  "suppliers_in_region": len(ai.suppliers_for(key, self.loc.region))}
        if note:
            result["note"] = note
        return result

    def tool_add_supplier(self, name: str, ingredient: str, price_per_unit: float, delivery_days: float,
                          delivery_fee: float = 0.0, case_size: Optional[float] = None,
                          free_shipping_at: Optional[float] = None, reliability_score: float = 0.9):
        ing = self._ingredient(ingredient)
        row = db.upsert_supplier(ai.DB_SESSION, None, {
            "name": name, "ingredient_name": ing, "price_per_unit": price_per_unit,
            "delivery_days": delivery_days, "reliability_score": reliability_score,
            "delivery_fee": delivery_fee, "free_shipping_at": free_shipping_at, "min_order": 1.0,
            "case_size": case_size, "notes": "added by AI chat", "region_code": self.loc.region,
        })
        ai.reload_from_db()
        return {"status": "added", "supplier_id": row.id, "name": row.name, "ingredient": ing}

    def tool_punch_sale(self, dish: str, servings: int):
        name = ai.resolve_dish_name(dish)
        if name is None:
            raise ToolError(f"Unknown dish '{dish}'. Dishes: {ai.list_dishes()}")
        loc, recipe = self.loc, ai.get_recipe(name)
        missing = [i for i in recipe if not loc.inventory.get(i)]
        if missing:
            raise ToolError(f"{name} uses {', '.join(missing)}, not in the inventory. Sale not recorded.")
        before = len(loc.order_history)
        loc.punch_sale(name, int(servings))
        return {"status": "recorded", "dish": name, "servings": int(servings),
                "stock_after": {i: round(loc.inventory.get(i).stock, 3) for i in recipe},
                "auto_orders": [_order_summary(o) for o in loc.order_history[before:]]}

    def tool_list_orders(self, status: Optional[str] = None):
        return [_order_summary(o) for o in reversed(self.loc.order_history)
                if status is None or o.status == status][:30]

    def tool_receive_order(self, order_id: str):
        return self.loc.receive_order(self._own_order(order_id).order_id)

    def tool_cancel_order(self, order_id: str):
        return self.loc.cancel_order(self._own_order(order_id).order_id)


# ═══════════════════════════════════════════════════════════════
# 3. AGENT LOOP
# ═══════════════════════════════════════════════════════════════

def _clean_history(history: Optional[List[dict]]) -> List[dict]:
    """Keep plain-text user/assistant turns, starting with a user turn."""
    turns = [{"role": h["role"], "content": str(h["content"])} for h in (history or [])
             if h.get("role") in ("user", "assistant") and str(h.get("content", "")).strip()]
    turns = turns[-MAX_HISTORY_MESSAGES:]
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns


def chat(location_key: str, message: str, history: Optional[List[dict]] = None,
         guard: Callable = _capture, client=None) -> dict:
    """Run one staff message through Claude with the inventory tools.
    Returns {"reply": str, "log": [...]}. API errors are raised to the caller."""
    client = client or make_client()
    toolbox = ToolBox(location_key, guard)
    messages = _clean_history(history) + [{"role": "user", "content": message}]
    loc = ai.LOCATIONS[location_key]
    system = SYSTEM_PROMPT.format(location=loc.name, region=loc.region or "not set",
                                  today=f"{datetime.now():%Y-%m-%d}")

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": EFFORT},
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "refusal":
            return {"reply": "Sorry, I can't help with that request.", "log": toolbox.log}

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break

        results = []
        for block in response.content:
            if block.type == "tool_use":
                output, is_error = toolbox.run(block.name, block.input)
                result = {"type": "tool_result", "tool_use_id": block.id, "content": output}
                if is_error:
                    result["is_error"] = True
                results.append(result)
        messages.append({"role": "user", "content": results})   # all results in ONE message
    else:
        return {"reply": "I had to stop: that request needed too many steps. Please split it up.",
                "log": toolbox.log}

    reply = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if response.stop_reason == "max_tokens":
        reply += "\n(reply cut short)"
    return {"reply": reply or "Done.", "log": toolbox.log}
