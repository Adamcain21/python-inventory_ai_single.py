"""
Inventory AI — v3 (SQLite edition)
==================================
Automatic kitchen inventory + ordering system, multi-location capable.

Features:
  • Punch a sale -> ingredients deducted automatically
  • Stock falls below reorder point -> auto-order from best supplier
  • Compare all suppliers on REAL total cost (price + delivery fee,
    respecting free-shipping thresholds), speed/reliability break ties
  • Text the AI in natural language, multiple ingredients in one message
  • Never requires manual approval -- but has real safety nets:
      - capacity cap (blocks unrealistic "10,000 people" requests)
      - weekly budget cap (blocks overspending)
      - duplicate-order cooldown (won't double-order the same thing)
      - spoilage warning (won't silently over-order perishables)
      - case/pack-size rounding (orders in real supplier units)
  • Orders are PENDING until received; stock only updates on receipt
  • Orders can be cancelled/undone while still pending
  • Notifications log every action for the owner to review
  • Multi-location: run more than one restaurant/kitchen from one program
  • Everything is stored in SQLite (inventory.db) -- every sale, order,
    receipt and cancellation is saved immediately (see db.py)

Run:  python inventory_ai.py
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import os
import re
import math

from sqlalchemy import select

import db


# ═══════════════════════════════════════════════════════════════
# 1. INGREDIENTS
# ═══════════════════════════════════════════════════════════════

@dataclass
class Ingredient:
    name: str
    unit: str
    stock: float
    reorder_point: float
    reorder_qty: float
    min_order_qty: float = 1.0
    shelf_life_days: Optional[float] = None      # None = doesn't spoil (e.g. flour, oil)
    daily_usage_estimate: float = 0.0            # typical amount used per day, for spoilage math
    last_ordered: Optional[datetime] = None

    def __post_init__(self):
        self.name = self.name.lower().strip()


class Inventory:
    def __init__(self):
        self.items: Dict[str, Ingredient] = {}

    def add_ingredient(self, name, unit, stock, reorder_point, reorder_qty,
                        min_order_qty=1.0, shelf_life_days=None, daily_usage_estimate=0.0):
        key = name.lower().strip()
        self.items[key] = Ingredient(
            key, unit, stock, reorder_point, reorder_qty, min_order_qty,
            shelf_life_days, daily_usage_estimate,
        )

    def get(self, name: str) -> Optional[Ingredient]:
        return self.items.get(name.lower().strip())

    def deduct(self, name: str, amount: float) -> float:
        key = name.lower().strip()
        if key not in self.items:
            raise ValueError(f"Unknown ingredient: {name}")
        self.items[key].stock = max(0.0, self.items[key].stock - amount)
        return self.items[key].stock

    def add_stock(self, name: str, amount: float):
        key = name.lower().strip()
        if key not in self.items:
            raise ValueError(f"Unknown ingredient: {name}")
        self.items[key].stock += amount
        self.items[key].last_ordered = datetime.now()

    def needs_reorder(self, name: str) -> bool:
        item = self.get(name)
        return bool(item and item.stock < item.reorder_point)

    def status(self) -> str:
        lines = ["=== CURRENT INVENTORY ==="]
        for name, item in sorted(self.items.items()):
            flag = "  LOW" if item.stock < item.reorder_point else ""
            lines.append(
                f"  {name:20} {item.stock:8.2f} {item.unit:6} "
                f"(reorder @ {item.reorder_point:g}){flag}"
            )
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 2. RECIPES  (shared across every location -- loaded from the DB)
# ═══════════════════════════════════════════════════════════════

RECIPES: Dict[str, Dict[str, float]] = {}      # filled by load_from_db()
DISH_ALIASES: Dict[str, str] = {}              # filled by load_from_db()


def resolve_dish_name(dish: str) -> Optional[str]:
    key = dish.lower().strip().replace(" ", "_")
    if key in RECIPES:
        return key
    alias = DISH_ALIASES.get(dish.lower().strip())
    if alias and alias in RECIPES:
        return alias
    return None


def get_recipe(dish: str) -> Dict[str, float]:
    key = resolve_dish_name(dish)
    if key is None:
        raise ValueError(f"Unknown dish: {dish}. Available: {list(RECIPES.keys())}")
    return RECIPES[key]


def list_dishes() -> List[str]:
    return list(RECIPES.keys())


def _find_dish_for_ingredient(ingredient: str) -> Tuple[Optional[str], Optional[float]]:
    for dish, recipe in RECIPES.items():
        if ingredient in recipe:
            return dish, recipe[ingredient]
    return None, None


# ═══════════════════════════════════════════════════════════════
# 3. SUPPLIERS  (shared across every location -- loaded from the DB)
# ═══════════════════════════════════════════════════════════════

@dataclass
class Supplier:
    name: str
    price_per_unit: float
    delivery_days: float
    reliability_score: float  # 0.0 - 1.0
    delivery_fee: float = 0.0
    free_shipping_at: Optional[float] = None   # order qty at/above which fee is waived
    min_order: float = 1.0
    case_size: Optional[float] = None          # e.g. sells in cases of 24 -- None = sells any amount
    notes: str = ""
    id: Optional[int] = None                   # row id in the suppliers table
    region: Optional[str] = None               # "QC", "ON"... None = serves every region
    price_source: str = "confirmed"            # "estimate" until the restaurant confirms the real price
    price_updated_at: Optional[datetime] = None  # date of the invoice/price list the price comes from

    def round_to_case(self, quantity: float) -> float:
        """Round a raw needed quantity up to a whole number of cases, if this
        supplier only sells by the case (e.g. 24-packs)."""
        if self.case_size:
            cases = math.ceil(quantity / self.case_size)
            return cases * self.case_size
        return quantity

    def total_cost(self, quantity: float) -> float:
        qty = self.round_to_case(quantity)
        fee = self.delivery_fee
        if self.free_shipping_at is not None and qty >= self.free_shipping_at:
            fee = 0.0
        return qty * self.price_per_unit + fee


SUPPLIERS: Dict[str, List[Supplier]] = {}      # filled by load_from_db()


def suppliers_for(ingredient: str, region: Optional[str] = None) -> List[Supplier]:
    """Offers for an ingredient that serve this region (region None = every offer)."""
    offers = SUPPLIERS.get(ingredient.lower().strip(), [])
    return [s for s in offers if region is None or s.region in (None, region)]


def choose_supplier(ingredient: str, quantity: float, prefer_speed: bool = False,
                     tie_margin: float = 0.08, region: Optional[str] = None) -> Supplier:
    """Cost-first supplier choice: cheapest REAL total cost wins; speed and
    reliability only break ties within `tie_margin` (default 8%).
    Only suppliers serving `region` are considered."""
    candidates = suppliers_for(ingredient, region)
    if not candidates:
        where = f" in region {region}" if region else ""
        raise ValueError(f"No suppliers configured for '{ingredient}'{where}")

    viable = [s for s in candidates if quantity >= s.min_order] or candidates
    costed = [(s, s.total_cost(quantity)) for s in viable]
    cheapest_cost = min(c for _, c in costed)

    margin = tie_margin * 1.5 if prefer_speed else tie_margin
    threshold = cheapest_cost * (1 + margin)
    price_competitive = [s for s, c in costed if c <= threshold]

    return max(price_competitive, key=_tiebreak)


def _tiebreak(s: Supplier) -> float:
    return s.reliability_score * 0.6 + (1.0 / max(s.delivery_days, 0.1)) * 0.4


# ═══════════════════════════════════════════════════════════════
# 3b. PURCHASE PLANNER  (several items -> one delivery per supplier)
# ═══════════════════════════════════════════════════════════════

@dataclass
class SupplierTerms:
    """Delivery conditions of one supplier in one region: charged once per DELIVERY."""
    supplier: str
    region: Optional[str]
    min_order_value: float = 0.0              # $ of goods needed for the supplier to deliver
    delivery_fee: float = 0.0                 # per delivery
    free_delivery_over: Optional[float] = None  # $ of goods at/above which the fee is waived
    price_source: str = "confirmed"           # "estimate" (catalog) or "confirmed" (restaurant)


SUPPLIER_TERMS: Dict[Tuple[str, Optional[str]], SupplierTerms] = {}   # filled by load_from_db()


def terms_for(supplier_name: str, region: Optional[str]) -> Optional[SupplierTerms]:
    return SUPPLIER_TERMS.get((supplier_name, region)) or SUPPLIER_TERMS.get((supplier_name, None))


@dataclass
class PlanLine:
    ingredient: str
    requested: float
    quantity: float        # after case rounding / supplier minimum quantity
    supplier: Supplier
    cost: float            # goods only, no delivery fee


@dataclass
class PlanGroup:
    """Everything bought from one supplier = one delivery."""
    supplier_name: str
    lines: List[PlanLine]
    subtotal: float
    delivery_fee: float
    base_delivery_fee: float
    min_order_value: float
    terms_estimated: bool
    joins_open_delivery: bool = False   # added to a delivery already ordered today from this supplier

    @property
    def total(self) -> float:
        return self.subtotal + self.delivery_fee

    @property
    def below_minimum(self) -> bool:
        return self.subtotal + 1e-9 < self.min_order_value

    def to_dict(self) -> dict:
        return {"supplier": self.supplier_name, "subtotal": round(self.subtotal, 2),
                "delivery_fee": round(self.delivery_fee, 2), "total": round(self.total, 2),
                "min_order_value": self.min_order_value, "below_minimum": self.below_minimum,
                "missing_for_minimum": round(max(0.0, self.min_order_value - self.subtotal), 2),
                "terms_estimated": self.terms_estimated, "joins_open_delivery": self.joins_open_delivery,
                "lines": [{"ingredient": l.ingredient, "requested": l.requested, "quantity": l.quantity,
                           "unit_price": l.supplier.price_per_unit, "cost": round(l.cost, 2),
                           "price_estimated": l.supplier.price_source == "estimate"} for l in self.lines]}


@dataclass
class PurchasePlan:
    groups: List[PlanGroup]
    unavailable: List[str]           # no supplier in the region

    @property
    def total(self) -> float:
        return round(sum(g.total for g in self.groups), 2)

    @property
    def feasible(self) -> bool:
        return not any(g.below_minimum for g in self.groups)

    def to_dict(self) -> dict:
        return {"total": self.total, "deliveries": len(self.groups), "feasible": self.feasible,
                "groups": [g.to_dict() for g in self.groups], "unavailable": self.unavailable}


def _make_group(name: str, region: Optional[str], lines: List[PlanLine],
                open_deliveries: Optional[Dict[str, float]] = None) -> PlanGroup:
    subtotal = sum(l.cost for l in lines)
    terms = terms_for(name, region)
    if open_deliveries and name in open_deliveries:
        # joins a delivery already ordered today: its fee is paid and its minimum reached
        return PlanGroup(name, lines, subtotal, 0.0, 0.0, 0.0, terms_estimated=False, joins_open_delivery=True)
    if terms:
        base_fee = terms.delivery_fee
        free = terms.free_delivery_over is not None and subtotal >= terms.free_delivery_over
        fee, minimum = (0.0 if free else base_fee), terms.min_order_value
    else:   # no delivery terms: each offer's own fee / free-shipping quantity, charged once per delivery
        fees = [0.0 if (l.supplier.free_shipping_at is not None and l.quantity >= l.supplier.free_shipping_at)
                else l.supplier.delivery_fee for l in lines]
        fee, base_fee, minimum = max(fees), max(l.supplier.delivery_fee for l in lines), 0.0
    return PlanGroup(name, lines, subtotal, fee, base_fee, minimum,
                     terms_estimated=bool(terms and terms.price_source == "estimate"))


def plan_purchase(items: Dict[str, float], region: Optional[str] = None, prefer_speed: bool = False,
                  tie_margin: float = 0.08, open_deliveries: Optional[Dict[str, float]] = None) -> PurchasePlan:
    """Best way to buy several items: cheapest REAL total (goods + one delivery fee per
    supplier), every supplier's minimum order respected. Among plans within `tie_margin`
    of the cheapest, fewer deliveries win, then faster / more reliable suppliers.
    open_deliveries: {supplier: $ already ordered today} -- adding to those is free.
    For a single item this gives the same supplier as choose_supplier()."""
    offers: Dict[str, Dict[str, Supplier]] = {}
    unavailable = []
    for ing, qty in items.items():
        candidates = suppliers_for(ing, region)
        if not candidates:
            unavailable.append(ing)
            continue
        viable = [s for s in candidates if qty >= s.min_order] or candidates
        offers[ing] = {}
        for s in sorted(viable, key=lambda s: s.region is not None):   # region-specific offer wins a name clash
            offers[ing][s.name] = s
    if not offers:
        return PurchasePlan([], unavailable)

    def build(assign: Dict[str, str]) -> PurchasePlan:
        by_supplier: Dict[str, List[PlanLine]] = {}
        for ing, name in assign.items():
            s = offers[ing][name]
            qty = max(s.round_to_case(items[ing]), s.min_order)
            by_supplier.setdefault(name, []).append(PlanLine(ing, items[ing], qty, s, qty * s.price_per_unit))
        groups = [_make_group(name, region, lines, open_deliveries) for name, lines in sorted(by_supplier.items())]
        return PurchasePlan(groups, unavailable)

    def penalty(plan: PurchasePlan) -> float:   # infeasible plans only win if nothing is feasible
        return plan.total + sum(1e6 + g.min_order_value - g.subtotal for g in plan.groups if g.below_minimum)

    ings = list(offers)
    alone = {ing: min(offers[ing], key=lambda n: build({ing: n}).total) for ing in ings}
    candidates = [build(alone)]
    for name in set.intersection(*(set(offers[i]) for i in ings)):        # one supplier for everything
        candidates.append(build({i: name for i in ings}))

    assign, best = dict(alone), build(alone)                              # local search: move / merge
    for _ in range(50):
        moves = [{ing: name} for ing in ings for name in offers[ing] if name != assign[ing]]
        used = set(assign.values())
        for a in used:
            group_a = [i for i, n in assign.items() if n == a]
            moves += [{i: b for i in group_a} for b in used if b != a and all(b in offers[i] for i in group_a)]
        improved = False
        for move in moves:
            trial = {**assign, **move}
            plan = build(trial)
            if penalty(plan) < penalty(best) - 1e-9:
                assign, best, improved = trial, plan, True
        if not improved:
            break
    candidates.append(best)

    feasible = [p for p in candidates if p.feasible]
    if not feasible:
        return min(candidates, key=penalty)
    cheapest = min(p.total for p in feasible)
    margin = tie_margin * 1.5 if prefer_speed else tie_margin
    close = [p for p in feasible if p.total <= cheapest * (1 + margin) + 1e-9]
    # fewer NEW deliveries first (joining today's open delivery doesn't add a truck)
    return max(close, key=lambda p: (-sum(not g.joins_open_delivery for g in p.groups),
                                     sum(_tiebreak(g.lines[0].supplier) for g in p.groups) / len(p.groups)))


# ═══════════════════════════════════════════════════════════════
# 4. ORDERS
# ═══════════════════════════════════════════════════════════════

@dataclass
class Order:
    order_id: str
    ingredient: str
    quantity: float
    supplier: Supplier
    trigger: str
    unit_price: float
    total_cost: float
    placed_at: datetime = field(default_factory=datetime.now)
    expected_arrival: datetime = field(default_factory=datetime.now)
    status: str = "pending"   # pending -> received, or -> cancelled
    po_id: Optional[str] = None   # purchase order grouping several lines of one delivery


@dataclass
class PurchaseOrder:
    """One delivery from one supplier with several lines. Its delivery fee is paid once."""
    po_id: str
    supplier_name: str
    delivery_fee: float
    placed_at: datetime = field(default_factory=datetime.now)


@dataclass
class BasketResult:
    orders: List[Order] = field(default_factory=list)
    purchase_orders: List[PurchaseOrder] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)   # (ingredient, reason)
    total_cost: float = 0.0


_order_counter = 0   # resumed from the highest ORD-xxxxx in the DB by load_from_db()
_po_counter = 0      # resumed from the highest PO-xxxxx


def _next_id() -> str:
    global _order_counter
    _order_counter += 1
    return f"ORD-{_order_counter:05d}"


def _next_po_id() -> str:
    global _po_counter
    _po_counter += 1
    return f"PO-{_po_counter:05d}"


# ═══════════════════════════════════════════════════════════════
# 5. FORECASTING
# ═══════════════════════════════════════════════════════════════

DAY_OF_WEEK_MULTIPLIER = {0: 0.90, 1: 0.90, 2: 1.00, 3: 1.05, 4: 1.40, 5: 1.60, 6: 1.25}


def forecast_ingredient_need(dish: str, ingredient: str, base_daily_servings: float, target_date: datetime) -> float:
    recipe = RECIPES.get(dish, {})
    per_serving = recipe.get(ingredient, 0.0)
    multiplier = DAY_OF_WEEK_MULTIPLIER.get(target_date.weekday(), 1.0)
    return per_serving * base_daily_servings * multiplier


def next_weekday(weekday: int) -> datetime:
    today = datetime.now()
    days_ahead = (weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=days_ahead)


# ═══════════════════════════════════════════════════════════════
# 6. LOCATION  (one restaurant/kitchen: inventory + orders + safety limits)
# ═══════════════════════════════════════════════════════════════

class CapacityExceededError(Exception):
    pass


class BudgetExceededError(Exception):
    pass


class DuplicateOrderError(Exception):
    pass


class MinimumOrderError(Exception):
    """The supplier doesn't deliver below its minimum order value."""


class Location:
    def __init__(self, name: str, max_capacity: int, weekly_budget: float,
                 max_days_to_stock_ahead: int = 3, order_cooldown_hours: float = 4.0):
        self.name = name
        self.inventory = Inventory()
        self.order_history: List[Order] = []
        self.purchase_orders: Dict[str, PurchaseOrder] = {}
        self.notifications: List[str] = []

        # --- safety limits (edit per real restaurant) ---
        self.max_capacity = max_capacity
        self.max_days_to_stock_ahead = max_days_to_stock_ahead
        self.max_people_per_request = max_capacity * max_days_to_stock_ahead
        self.weekly_budget = weekly_budget
        self.spend_this_week = 0.0
        self.order_cooldown_hours = order_cooldown_hours
        self._last_order_time: Dict[str, datetime] = {}   # ingredient -> last order time

        self.region: Optional[str] = None   # "QC", "ON"... only suppliers of this region are used

        # --- persistence (set by load_from_db; None = in-memory only) ---
        self.db_id: Optional[int] = None
        self.store: Optional[db.Store] = None

    # ---------- safety checks ----------

    def check_capacity(self, people: int):
        if people > self.max_people_per_request:
            raise CapacityExceededError(
                f"Request is for {people} people, but {self.name}'s safety cap is "
                f"{self.max_people_per_request} people per request "
                f"(max capacity {self.max_capacity}/day x {self.max_days_to_stock_ahead} days). "
                f"This looks like a mistake or typo -- no order was placed."
            )

    def check_budget(self, cost: float):
        if self.spend_this_week + cost > self.weekly_budget:
            raise BudgetExceededError(
                f"This ${cost:.2f} order would push {self.name}'s spending to "
                f"${self.spend_this_week + cost:.2f}, over the ${self.weekly_budget:.2f} weekly budget "
                f"(${self.spend_this_week:.2f} already spent this week). No order was placed."
            )

    def check_duplicate(self, ingredient: str):
        last = self._last_order_time.get(ingredient)
        if last and (datetime.now() - last) < timedelta(hours=self.order_cooldown_hours):
            minutes_ago = (datetime.now() - last).total_seconds() / 60
            raise DuplicateOrderError(
                f"{ingredient} was already ordered {minutes_ago:.0f} minute(s) ago, within the "
                f"{self.order_cooldown_hours}h duplicate-order cooldown. Skipped to avoid double-ordering. "
                f"(Use 'force order' if this is intentional.)"
            )

    def spoilage_warning(self, ingredient: str, qty_ordered: float) -> Optional[str]:
        item = self.inventory.get(ingredient)
        if not item or not item.shelf_life_days or not item.daily_usage_estimate:
            return None
        total_on_hand_after = item.stock + qty_ordered
        days_to_use_it_all = total_on_hand_after / item.daily_usage_estimate
        if days_to_use_it_all > item.shelf_life_days:
            return (
                f"NOTE: at ~{item.daily_usage_estimate:g} {item.unit}/day usage, this much {ingredient} "
                f"would take ~{days_to_use_it_all:.1f} days to use, but it only keeps "
                f"{item.shelf_life_days:g} days -- some may spoil before use."
            )
        return None

    # ---------- notifications ----------

    def notify(self, message: str):
        stamped = f"[{datetime.now():%Y-%m-%d %H:%M}] {message}"
        self.notifications.append(stamped)
        if self.store:
            self.store.save_notification(self, stamped)
        print(f"  >> NOTIFICATION to {self.name} owner: {message}")

    # ---------- ordering ----------

    def place_order(self, ingredient: str, quantity: float, trigger: str,
                     force: bool = False, prefer_speed: bool = False, allow_below_minimum: bool = False) -> Order:
        """One ingredient. Same safety nets as always, plus the supplier's minimum order."""
        if quantity <= 0:
            raise ValueError("Order quantity must be positive")

        if not force:
            self.check_duplicate(ingredient)

        result = self.place_basket({ingredient: quantity}, trigger, force=True, prefer_speed=prefer_speed,
                                   allow_below_minimum=allow_below_minimum)
        return result.orders[0]

    def has_pending_order(self, ingredient: str) -> bool:
        return any(o.ingredient == ingredient and o.status == "pending" for o in self.order_history)

    def open_deliveries(self) -> Dict[str, List[Order]]:
        """Pending orders placed TODAY, per supplier: new items for that supplier can join
        the same delivery (no second delivery fee, minimum already reached)."""
        today = datetime.now().date()
        out: Dict[str, List[Order]] = {}
        for o in self.order_history:
            if o.status == "pending" and o.placed_at.date() == today:
                out.setdefault(o.supplier.name, []).append(o)
        return out

    def open_delivery_values(self) -> Dict[str, float]:
        return {name: sum(o.total_cost for o in orders) for name, orders in self.open_deliveries().items()}

    def place_basket(self, items: Dict[str, float], trigger: str, force: bool = False,
                     prefer_speed: bool = False, allow_below_minimum: bool = False,
                     partial: bool = False) -> BasketResult:
        """Several ingredients at once, grouped into ONE delivery per supplier (delivery fee
        paid once, supplier minimum order respected), via plan_purchase().
        partial=False: any problem (cooldown, no supplier, minimum, budget) raises, nothing is ordered.
        partial=True : order what can be ordered, return the rest in result.skipped."""
        result = BasketResult()
        wanted: Dict[str, float] = {}
        for name, qty in items.items():
            key = name.lower().strip()
            if qty <= 0:
                raise ValueError("Order quantity must be positive")
            if not force:
                try:
                    self.check_duplicate(key)
                except DuplicateOrderError as e:
                    if not partial:
                        raise
                    result.skipped.append((key, str(e)))
                    continue
            wanted[key] = wanted.get(key, 0.0) + qty
        if not wanted:
            return result

        open_now = self.open_deliveries()
        plan = plan_purchase(wanted, self.region, prefer_speed=prefer_speed,
                             open_deliveries={n: sum(o.total_cost for o in os_) for n, os_ in open_now.items()})
        for ing in plan.unavailable:
            msg = f"No suppliers configured for '{ing}'" + (f" in region {self.region}" if self.region else "")
            if not partial:
                raise ValueError(msg)
            result.skipped.append((ing, msg))

        groups = []
        for g in plan.groups:
            if g.below_minimum and not allow_below_minimum:
                msg = (f"{g.supplier_name} delivers from ${g.min_order_value:.2f} of goods, this order is only "
                       f"${g.subtotal:.2f} ({', '.join(l.ingredient for l in g.lines)}). Add items for this "
                       f"supplier or wait for more to order. No order was placed.")
                if not partial:
                    raise MinimumOrderError(msg)
                result.skipped += [(l.ingredient, msg) for l in g.lines]
                continue
            groups.append(g)

        if not partial:
            self.check_budget(round(sum(g.total for g in groups), 2))   # raises before anything is committed
        else:
            fitting, running = [], 0.0
            for g in sorted(groups, key=lambda g: g.total):
                try:
                    self.check_budget(round(running + g.total, 2))
                    fitting.append(g)
                    running += g.total
                except BudgetExceededError as e:
                    result.skipped += [(l.ingredient, str(e)) for l in g.lines]
            groups = fitting

        for g in groups:
            self._place_group(g, trigger, result, open_now.get(g.supplier_name))
        if self.store and result.orders:
            self.store.save_location(self)
        return result

    def _place_group(self, g: PlanGroup, trigger: str, result: BasketResult,
                     open_orders: Optional[List[Order]] = None):
        joining = bool(g.joins_open_delivery and open_orders)
        single = len(g.lines) == 1 and not joining
        po = None
        if joining:
            # add to today's delivery from this supplier: reuse its purchase order, or turn the
            # single order already placed into one (its fee is already in that order's cost)
            po_id = next((o.po_id for o in open_orders if o.po_id), None)
            if po_id:
                po = self.purchase_orders[po_id]
            else:
                po = PurchaseOrder(_next_po_id(), g.supplier_name, 0.0)
                self.purchase_orders[po.po_id] = po
                if self.store:
                    self.store.save_purchase_order(self, po)
                for o in open_orders:
                    o.po_id = po.po_id
                    if self.store:
                        self.store.save_order(self, o)
            result.purchase_orders.append(po)
            print(f"\n[ADDED TO TODAY'S DELIVERY] {po.po_id} - {g.supplier_name}: +{len(g.lines)} line(s), "
                  f"${g.subtotal:.2f}, no extra delivery fee")
        elif not single:
            po = PurchaseOrder(_next_po_id(), g.supplier_name, round(g.delivery_fee, 2))
            self.purchase_orders[po.po_id] = po
            self.spend_this_week += po.delivery_fee
            if self.store:
                self.store.save_purchase_order(self, po)
            result.purchase_orders.append(po)
            print(f"\n[PURCHASE ORDER] {po.po_id} - {g.supplier_name}: {len(g.lines)} lines, "
                  f"goods ${g.subtotal:.2f} + delivery ${g.delivery_fee:.2f} (paid once) = ${g.total:.2f}")

        for line in g.lines:
            supplier, qty = line.supplier, line.quantity
            cost = round(g.total, 2) if single else round(line.cost, 2)
            spoil_note = self.spoilage_warning(line.ingredient, qty)
            order = Order(
                order_id=_next_id(),
                ingredient=line.ingredient,
                quantity=qty,
                supplier=supplier,
                trigger=trigger,
                unit_price=supplier.price_per_unit,
                total_cost=cost,
                expected_arrival=datetime.now() + timedelta(days=supplier.delivery_days),
                po_id=po.po_id if po else None,
            )
            self.order_history.append(order)
            self.spend_this_week += cost
            self._last_order_time[line.ingredient] = datetime.now()
            if self.store:
                self.store.save_order(self, order)
            result.orders.append(order)

            if single:
                free_shipping_hit = g.delivery_fee == 0.0 and g.base_delivery_fee > 0.0
                fee_line = f"${g.delivery_fee:.2f}" + ("  (FREE SHIPPING)" if free_shipping_hit else "")
            else:
                fee_line = f"in {po.po_id} (paid once for the whole delivery)"
            print(
                f"\n[ORDER PLACED - PENDING] {order.order_id}\n"
                f"  Ingredient    : {qty:.2f} of {line.ingredient}\n"
                f"  Supplier      : {supplier.name}\n"
                f"  Price         : ${supplier.price_per_unit:.2f}/unit x {qty:.2f} = ${qty * supplier.price_per_unit:.2f}\n"
                f"  Delivery fee  : {fee_line}\n"
                f"  TOTAL COST    : ${order.total_cost:.2f}\n"
                f"  Expected      : {order.expected_arrival:%Y-%m-%d} ({supplier.delivery_days:g} day(s))\n"
                f"  Reliability   : {supplier.reliability_score:.0%}\n"
                f"  Trigger       : {trigger}\n"
                f"  Week spend    : ${self.spend_this_week:.2f} / ${self.weekly_budget:.2f}\n"
            )
            if spoil_note:
                print(f"  ! {spoil_note}")
            if single:
                self.notify(
                    f"Ordered {qty:.2f} {line.ingredient} from {supplier.name} (${cost:.2f}), "
                    f"arriving ~{order.expected_arrival:%b %d}. [{order.order_id}]"
                    + (f" {spoil_note}" if spoil_note else "")
                )
        if po:
            items = ", ".join(f"{l.quantity:g} {l.ingredient}" for l in g.lines)
            if joining:
                self.notify(f"Added to today's {g.supplier_name} delivery: {items} "
                            f"(${g.subtotal:.2f}, no extra delivery fee). [{po.po_id}]")
            else:
                self.notify(f"Ordered from {g.supplier_name} in one delivery: {items} "
                            f"(${g.total:.2f} incl. ${g.delivery_fee:.2f} delivery). [{po.po_id}]")
        result.total_cost = round(result.total_cost + g.total, 2)

    def receive_purchase_order(self, po_id: str) -> str:
        lines = [o for o in self.order_history if o.po_id == po_id and o.status == "pending"]
        if po_id not in self.purchase_orders:
            return f"No purchase order with id {po_id}."
        if not lines:
            return f"{po_id} has nothing left to receive."
        for o in lines:
            self.receive_order(o.order_id)
        return f"{po_id}: {len(lines)} line(s) received. Stock updated."

    def receive_order(self, order_id: str) -> str:
        order = next((o for o in self.order_history if o.order_id == order_id), None)
        if not order:
            return f"No order with id {order_id}."
        if order.status != "pending":
            return f"Order {order_id} is already {order.status}, nothing to receive."
        order.status = "received"
        self.inventory.add_stock(order.ingredient, order.quantity)
        if self.store:
            self.store.save_order(self, order)
            self.store.save_ingredient(self, self.inventory.get(order.ingredient))
        self.notify(f"Received {order.quantity:.2f} {order.ingredient} from {order.supplier.name}. [{order_id}]")
        return f"Order {order_id} marked received. Stock updated."

    def cancel_order(self, order_id: str) -> str:
        order = next((o for o in self.order_history if o.order_id == order_id), None)
        if not order:
            return f"No order with id {order_id}."
        if order.status == "received":
            return f"Order {order_id} was already received -- can't cancel, stock already updated. Adjust stock manually if needed."
        if order.status == "cancelled":
            return f"Order {order_id} is already cancelled."
        order.status = "cancelled"
        self.spend_this_week = max(0.0, self.spend_this_week - order.total_cost)
        fee_note = ""
        po = self.purchase_orders.get(order.po_id) if order.po_id else None
        if po and po.delivery_fee and all(o.status == "cancelled" for o in self.order_history if o.po_id == po.po_id):
            # the whole delivery is cancelled: its delivery fee is refunded too
            self.spend_this_week = max(0.0, self.spend_this_week - po.delivery_fee)
            fee_note = f" Whole {po.po_id} cancelled: ${po.delivery_fee:.2f} delivery fee refunded too."
        if self.store:
            self.store.save_order(self, order)
            self.store.save_location(self)
        self.notify(f"CANCELLED order {order_id} ({order.quantity:.2f} {order.ingredient}, ${order.total_cost:.2f} refunded from weekly spend).{fee_note}")
        return f"Order {order_id} cancelled. Was still pending, so no stock/spend impact remains."

    def print_order_history(self):
        if not self.order_history:
            print("No orders placed yet.")
            return
        print(f"\n=== {self.name} ORDER HISTORY ===")
        for o in self.order_history:
            print(
                f"  {o.order_id} | {o.placed_at:%Y-%m-%d %H:%M} | {o.status:9} | "
                f"{o.quantity:.1f} {o.ingredient} from {o.supplier.name} "
                f"(${o.total_cost:.2f}) [{o.trigger}]"
            )

    def punch_sale(self, dish: str, servings: int = 1):
        try:
            recipe = get_recipe(dish)
        except ValueError as e:
            print(f"Error: {e}")
            return

        # check every ingredient exists BEFORE deducting anything
        missing = [i for i in recipe if not self.inventory.get(i)]
        if missing:
            print(f"Error: {dish} uses {', '.join(missing)}, which is not in {self.name}'s inventory. "
                  f"Sale not recorded.")
            return

        if self.store:
            self.store.save_sale(self, resolve_dish_name(dish), servings)

        print(f"\n--- {self.name}: punching sale: {servings} x {dish} ---")
        low: Dict[str, float] = {}
        for ingredient, per_serving in recipe.items():
            amount = per_serving * servings
            new_stock = self.inventory.deduct(ingredient, amount)
            item = self.inventory.get(ingredient)
            if self.store:
                self.store.save_ingredient(self, item)
            print(f"  - {amount:.2f} {item.unit} {ingredient}  ->  stock now {new_stock:.2f}")

            if self.inventory.needs_reorder(ingredient):
                print(f"  ! {ingredient} below reorder point ({item.reorder_point:g})")
                if not self.has_pending_order(ingredient):   # a delivery is already coming
                    low[ingredient] = item.reorder_qty

        if low:   # all low items together: one delivery per supplier, minimums respected
            result = self.place_basket(low, trigger="auto-low-stock", partial=True)
            for ingredient, reason in result.skipped:
                print(f"  AUTO-ORDER SKIPPED ({ingredient}): {reason}")

    def suggested_order(self) -> Tuple[Dict[str, float], Dict[str, float]]:
        """(needed, could_add): items below their reorder point with nothing on the way, and
        items getting close (< 1.5x reorder point) that can top up a delivery to its minimum."""
        needed, could_add = {}, {}
        for name, item in self.inventory.items.items():
            if self.has_pending_order(name) or item.reorder_qty <= 0:
                continue
            if item.stock < item.reorder_point:
                needed[name] = item.reorder_qty
            elif item.reorder_point > 0 and item.stock < item.reorder_point * 1.5:
                could_add[name] = item.reorder_qty
        return needed, could_add


# ═══════════════════════════════════════════════════════════════
# 7. NATURAL LANGUAGE INTERFACE  (multi-ingredient aware)
# ═══════════════════════════════════════════════════════════════

NL_PATTERNS = [
    re.compile(
        r"(?:i\s+)?need\s+(?P<item>[\w\s]+?)\s+for\s+(?P<people>\d+)\s+people"
        r"(?:\s+(?:this|on)\s+(?P<day>\w+))?",
        re.IGNORECASE,
    ),
    re.compile(
        r"order\s+(?:(?P<qty>\d+(?:\.\d+)?)\s+)?(?P<item>[\w\s]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:we\s+are\s+)?(?:low\s+on|running\s+out\s+of)\s+(?P<item>[\w\s]+)",
        re.IGNORECASE,
    ),
]

INGREDIENT_ALIASES: Dict[str, str] = {}        # filled by load_from_db()


def _normalize_item(raw: str) -> Optional[str]:
    text = raw.strip().lower()
    if text in INGREDIENT_ALIASES:
        return INGREDIENT_ALIASES[text]
    if text.endswith("s") and text[:-1] in INGREDIENT_ALIASES:
        return INGREDIENT_ALIASES[text[:-1]]
    from regional_catalog import exact_product   # French/English catalog words: "miel" -> honey
    return exact_product(text)


def _split_into_segments(text: str) -> List[str]:
    """Splits 'need carrots and chicken for 30 people' style requests into
    separate segments so each ingredient is processed on its own."""
    people_match = re.search(r"\bfor\s+(\d+)\s+people\b", text, re.IGNORECASE)
    if people_match:
        head = text[:people_match.start()]
        tail = text[people_match.start():]
        # strip a leading "need"/"i need" once, so we can re-add it per item
        head = re.sub(r"^\s*(?:i\s+)?need\s+", "", head.strip(), flags=re.IGNORECASE)
        items = re.split(r"\s*,\s*|\s+and\s+|\s*&\s*", head.strip())
        items = [i for i in items if i.strip()]
        if len(items) > 1:
            return [f"need {item} {tail}" for item in items]
    return [text]


def order_for_people(location: Location, ingredient: str, people: int, trigger: str = "human-request") -> str:
    """'Need <ingredient> for <people> people': capacity check, recipe math,
    then order the shortfall (at least the reorder quantity)."""
    item = location.inventory.get(ingredient)
    if not item:
        return f"'{ingredient}' is not in {location.name}'s inventory system yet."
    try:
        location.check_capacity(people)
    except CapacityExceededError as e:
        return f"SAFETY STOP: {e}"

    dish, per_serving = _find_dish_for_ingredient(ingredient)
    if per_serving is None:
        return f"No recipe currently uses '{ingredient}'."

    needed = per_serving * people
    current_stock = item.stock
    shortfall = needed - current_stock

    if shortfall <= 0:
        return f"Enough {ingredient} in stock ({current_stock:.2f} {item.unit}) for {people} people. No order needed."

    qty_to_order = max(shortfall, item.reorder_qty)
    try:
        location.place_order(ingredient, qty_to_order, trigger=trigger)
    except (BudgetExceededError, DuplicateOrderError, MinimumOrderError) as e:
        return f"ORDER BLOCKED: {e}"
    return f"Needed {needed:.2f} {item.unit} of {ingredient} for {people} people. Had {current_stock:.2f}, ordered {qty_to_order:.2f} more."


def _handle_single_segment(location: Location, text: str) -> str:
    text = text.strip()
    for pattern in NL_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue

        groups = match.groupdict()
        item_raw = re.sub(r"^(?:i\s+)?need\s+", "", groups.get("item", ""), flags=re.IGNORECASE).strip()
        ingredient = _normalize_item(item_raw)

        if not ingredient:
            return f"I don't recognize the ingredient '{item_raw}'."

        item = location.inventory.get(ingredient)
        if not item:
            return f"'{ingredient}' is not in {location.name}'s inventory system yet."

        # Case 1: "need X for N people"
        if groups.get("people"):
            return order_for_people(location, ingredient, int(groups["people"]))

        # Case 2: "order N X" or "order X"
        if "qty" in groups:
            qty = float(groups["qty"]) if groups.get("qty") else item.reorder_qty
            try:
                location.place_order(ingredient, qty, trigger="human-request")
            except (BudgetExceededError, DuplicateOrderError, MinimumOrderError) as e:
                return f"ORDER BLOCKED: {e}"
            return f"Ordered {qty:.2f} {item.unit} of {ingredient}."

        # Case 3: "low on X"
        qty = item.reorder_qty
        try:
            location.place_order(ingredient, qty, trigger="human-request")
        except (BudgetExceededError, DuplicateOrderError, MinimumOrderError) as e:
            return f"ORDER BLOCKED: {e}"
        return f"Ordered {qty:.2f} {item.unit} of {ingredient} (low-stock request)."

    return (
        "Sorry, I could not understand that request.\n"
        "Try: 'need green beans for 30 people' / 'order 50 carrots' / 'we are low on bread'"
    )


def handle_nl_request(location: Location, text: str) -> str:
    text = text.strip()
    if not text:
        return "Please type a request (e.g. 'need green beans for 30 people')."
    segments = _split_into_segments(text)
    replies = [_handle_single_segment(location, seg) for seg in segments]
    return "\n".join(replies)


# ═══════════════════════════════════════════════════════════════
# 8. LOAD FROM DATABASE + MULTI-LOCATION SETUP + CLI
# ═══════════════════════════════════════════════════════════════

LOCATIONS: Dict[str, Location] = {}            # filled by load_from_db()
DB_SESSION = None                              # session of the currently loaded database


def load_from_db(db_url: Optional[str] = None, session=None, catalog: Optional[bool] = None) -> Dict[str, Location]:
    """Load recipes, suppliers, locations, stock, orders and notifications
    from the database into memory. Seeds demo data if the DB is empty
    (catalog=True: real regional suppliers; False: small fictional set for tests;
    None: INVENTORY_CATALOG env var, default on).
    Returns (and fills in place) the module-level LOCATIONS dict."""
    global _order_counter, _po_counter, DB_SESSION
    if catalog is None:
        catalog = os.environ.get("INVENTORY_CATALOG", "1") != "0"
    session = session or db.open_session(db_url, catalog=catalog)
    session.expire_all()   # always read fresh rows (matters when reloading after config edits)
    DB_SESSION = session
    store = db.Store(session)

    RECIPES.clear()
    DISH_ALIASES.clear()
    for r in session.scalars(select(db.RecipeRow).order_by(db.RecipeRow.id)):
        RECIPES[r.name] = {it.ingredient_name: it.qty_per_serving for it in r.items}
        for alias in db.split_aliases(r.aliases):
            DISH_ALIASES[alias] = r.name

    SUPPLIERS.clear()
    suppliers_by_id: Dict[int, Supplier] = {}
    for s in session.scalars(select(db.SupplierRow).order_by(db.SupplierRow.id)):
        sup = Supplier(s.name, s.price_per_unit, s.delivery_days, s.reliability_score,
                       delivery_fee=s.delivery_fee, free_shipping_at=s.free_shipping_at,
                       min_order=s.min_order, case_size=s.case_size, notes=s.notes, id=s.id,
                       region=s.region_code, price_source=s.price_source, price_updated_at=s.price_updated_at)
        SUPPLIERS.setdefault(s.ingredient_name, []).append(sup)
        suppliers_by_id[s.id] = sup

    SUPPLIER_TERMS.clear()
    for t in session.scalars(select(db.SupplierTermsRow)):
        SUPPLIER_TERMS[(t.name, t.region_code)] = SupplierTerms(
            t.name, t.region_code, min_order_value=t.min_order_value, delivery_fee=t.delivery_fee,
            free_delivery_over=t.free_delivery_over, price_source=t.price_source)

    INGREDIENT_ALIASES.clear()
    LOCATIONS.clear()
    highest_order_number = highest_po_number = 0
    for row in session.scalars(select(db.LocationRow).order_by(db.LocationRow.id)):
        loc = Location(row.name, max_capacity=row.max_capacity, weekly_budget=row.weekly_budget,
                       max_days_to_stock_ahead=row.max_days_to_stock_ahead,
                       order_cooldown_hours=row.order_cooldown_hours)
        loc.db_id = row.id
        loc.region = row.region_code
        loc.spend_this_week = row.spend_this_week

        for i in row.ingredients:
            loc.inventory.add_ingredient(i.name, i.unit, i.stock, i.reorder_point, i.reorder_qty,
                                         min_order_qty=i.min_order_qty, shelf_life_days=i.shelf_life_days,
                                         daily_usage_estimate=i.daily_usage_estimate)
            loc.inventory.get(i.name).last_ordered = i.last_ordered
            for alias in db.split_aliases(i.aliases):
                INGREDIENT_ALIASES[alias] = i.name

        orders = session.scalars(select(db.OrderRow)
                                 .where(db.OrderRow.location_id == row.id)
                                 .order_by(db.OrderRow.order_id))
        for o in orders:
            loc.order_history.append(Order(
                order_id=o.order_id, ingredient=o.ingredient, quantity=o.quantity,
                supplier=suppliers_by_id[o.supplier_id], trigger=o.trigger,
                unit_price=o.unit_price, total_cost=o.total_cost,
                placed_at=o.placed_at, expected_arrival=o.expected_arrival, status=o.status, po_id=o.po_id,
            ))
            # rebuild the duplicate-order cooldown from the most recent order per ingredient
            last = loc._last_order_time.get(o.ingredient)
            if last is None or o.placed_at > last:
                loc._last_order_time[o.ingredient] = o.placed_at
            highest_order_number = max(highest_order_number, int(o.order_id.split("-")[1]))

        for p in session.scalars(select(db.PurchaseOrderRow).where(db.PurchaseOrderRow.location_id == row.id)):
            loc.purchase_orders[p.po_id] = PurchaseOrder(p.po_id, p.supplier_name, p.delivery_fee, p.placed_at)
            highest_po_number = max(highest_po_number, int(p.po_id.split("-")[1]))

        notes =session.scalars(select(db.NotificationRow)
                                .where(db.NotificationRow.location_id == row.id)
                                .order_by(db.NotificationRow.id))
        loc.notifications = [n.message for n in notes]

        loc.store = store   # attached last, so loading never writes back
        LOCATIONS[row.key] = loc

    _order_counter = highest_order_number
    _po_counter = highest_po_number
    return LOCATIONS


def reload_from_db() -> Dict[str, Location]:
    """Re-read everything from the already-open database (after config edits).
    Safe at any time: every action is already saved, so nothing is lost."""
    return load_from_db(session=DB_SESSION)


def show_help():
    print("""
Available commands:
  use <location>              Switch active location (e.g. use uptown)
  locations                   List all locations
  sale <dish> [servings]      Punch a sale
  dishes                      List all dishes
  stock                       Show current inventory
  order history                Show all orders (pending/received/cancelled)
  receive <order_id>          Mark a pending order as received (updates stock)
  cancel <order_id>           Cancel a pending order
  notifications                Show this location's notification log
  forecast <dish> <ing> <base> Forecast need
  text <message>               Talk to the AI in natural language
  force order <message>        Same as above, skips duplicate-order cooldown
  help                          Show this help
  quit / exit                   Leave the program

Natural language examples:
  need green beans for 30 people
  need carrots and chicken for 30 people
  order 50 carrots
  we are low on bread
""")


def main():
    load_from_db()
    active_name = "downtown" if "downtown" in LOCATIONS else next(iter(LOCATIONS))
    print("=" * 70)
    print("  INVENTORY AI v3  --  multi-location, safety nets, real supplier math")
    print("=" * 70)
    print(f"Database: {db.default_db_url()}")
    print(f"Locations available: {', '.join(LOCATIONS)}")
    print(f"Active location: {active_name}")
    print(LOCATIONS[active_name].inventory.status())
    show_help()

    while True:
        loc = LOCATIONS[active_name]
        try:
            raw = input(f"\n[{loc.name}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not raw:
            continue

        parts = raw.split(maxsplit=1)
        cmd = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            print("Bye.")
            break

        elif cmd == "help":
            show_help()

        elif cmd == "locations":
            for key, l in LOCATIONS.items():
                marker = " (active)" if key == active_name else ""
                print(f"  {key}: {l.name}{marker} -- cap {l.max_capacity}/day, "
                      f"budget ${l.spend_this_week:.2f}/${l.weekly_budget:.2f} this week")

        elif cmd == "use":
            if len(parts) < 2 or parts[1].lower() not in LOCATIONS:
                print(f"Usage: use <{'|'.join(LOCATIONS)}>")
                continue
            active_name = parts[1].lower()
            print(f"Switched to {LOCATIONS[active_name].name}.")

        elif cmd == "dishes":
            print("Available dishes:", ", ".join(list_dishes()))

        elif cmd == "stock":
            print(loc.inventory.status())

        elif cmd == "sale":
            if len(parts) < 2:
                print("Usage: sale <dish> [servings]")
                continue
            args = parts[1].split()
            dish = args[0]
            try:
                servings = int(args[1]) if len(args) > 1 else 1
            except ValueError:
                print(f"'{args[1]}' is not a whole number. Usage: sale <dish> [servings]")
                continue
            if servings <= 0:
                print("Servings must be at least 1.")
                continue
            loc.punch_sale(dish, servings)
            print(loc.inventory.status())

        elif cmd == "order" and len(parts) > 1 and parts[1].lower().startswith("history"):
            loc.print_order_history()

        elif cmd == "receive":
            if len(parts) < 2:
                print("Usage: receive <order_id>")
                continue
            print(loc.receive_order(parts[1].strip()))
            print(loc.inventory.status())

        elif cmd == "cancel":
            if len(parts) < 2:
                print("Usage: cancel <order_id>")
                continue
            print(loc.cancel_order(parts[1].strip()))

        elif cmd == "notifications":
            if not loc.notifications:
                print("No notifications yet.")
            else:
                print(f"\n=== {loc.name} NOTIFICATIONS ===")
                for n in loc.notifications:
                    print(f"  {n}")

        elif cmd == "forecast":
            if len(parts) < 2:
                print("Usage: forecast <dish> <ingredient> <base_daily_servings>")
                continue
            args = parts[1].split()
            if len(args) < 3:
                print("Usage: forecast <dish> <ingredient> <base_daily_servings>")
                continue
            try:
                dish, ingredient, base = args[0], args[1], float(args[2])
            except ValueError:
                print(f"'{args[2]}' is not a number. Usage: forecast <dish> <ingredient> <base_daily_servings>")
                continue
            friday = next_weekday(4)
            need = forecast_ingredient_need(dish, ingredient, base, friday)
            print(f"Forecast for next Friday ({friday:%Y-%m-%d}): {need:.2f} of {ingredient}")

        elif cmd == "text":
            if len(parts) < 2:
                print("Usage: text <your message>")
                continue
            print(handle_nl_request(loc, parts[1]))
            print(loc.inventory.status())

        elif cmd == "force" and len(parts) > 1 and parts[1].lower().startswith("order "):
            msg = parts[1][len("order "):]
            # bypass duplicate cooldown for this one request
            segments = _split_into_segments(msg)
            for seg in segments:
                try:
                    match = re.search(r"order\s+(?:(\d+(?:\.\d+)?)\s+)?([\w\s]+)", f"order {seg}", re.IGNORECASE)
                    if match:
                        qty = float(match.group(1)) if match.group(1) else None
                        item_raw = match.group(2).strip()
                        ingredient = _normalize_item(item_raw)
                        if ingredient:
                            item = loc.inventory.get(ingredient)
                            if not item:
                                print(f"ORDER BLOCKED: '{ingredient}' is not in {loc.name}'s inventory system yet.")
                                continue
                            qty = qty or item.reorder_qty
                            loc.place_order(ingredient, qty, trigger="human-request-forced", force=True)
                            continue
                        print(f"I don't recognize the ingredient '{item_raw}'.")
                except (BudgetExceededError, MinimumOrderError, ValueError) as e:
                    print(f"ORDER BLOCKED: {e}")
            print(loc.inventory.status())

        else:
            print(handle_nl_request(loc, raw))
            print(loc.inventory.status())


if __name__ == "__main__":
    main()
