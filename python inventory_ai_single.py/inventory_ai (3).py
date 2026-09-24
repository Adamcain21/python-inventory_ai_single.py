"""
Inventory AI — v3
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

Run:  python inventory_ai_v3.py
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import re
import math


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
                f"(reorder @ {item.reorder_point}){flag}"
            )
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 2. RECIPES  (shared across every location)
# ═══════════════════════════════════════════════════════════════

RECIPES: Dict[str, Dict[str, float]] = {
    "carrot_soup": {"carrot": 1.0, "oil": 0.05, "onion": 0.1, "bread": 0.05},
    "green_beans_side": {"green_beans": 0.15, "oil": 0.02, "garlic": 0.01},
    "roast_chicken": {"chicken": 1.0, "oil": 0.03, "garlic": 0.02, "onion": 0.1},
    "chicken_sandwich": {"chicken": 0.3, "bread": 0.15, "oil": 0.01, "lettuce": 0.05},
    "veggie_burger": {"bread": 0.15, "lettuce": 0.08, "onion": 0.05, "oil": 0.02, "green_beans": 0.05},
    "garlic_bread": {"bread": 0.2, "garlic": 0.03, "oil": 0.04},
}

DISH_ALIASES = {
    "carrot soup": "carrot_soup", "soup": "carrot_soup",
    "green beans": "green_beans_side", "beans": "green_beans_side",
    "roast chicken": "roast_chicken", "chicken": "roast_chicken",
    "chicken sandwich": "chicken_sandwich", "sandwich": "chicken_sandwich",
    "veggie burger": "veggie_burger", "burger": "veggie_burger",
    "garlic bread": "garlic_bread",
}


def get_recipe(dish: str) -> Dict[str, float]:
    key = dish.lower().strip().replace(" ", "_")
    if key in RECIPES:
        return RECIPES[key]
    alias = DISH_ALIASES.get(dish.lower().strip())
    if alias and alias in RECIPES:
        return RECIPES[alias]
    raise ValueError(f"Unknown dish: {dish}. Available: {list(RECIPES.keys())}")


def list_dishes() -> List[str]:
    return list(RECIPES.keys())


def _find_dish_for_ingredient(ingredient: str) -> Tuple[Optional[str], Optional[float]]:
    for dish, recipe in RECIPES.items():
        if ingredient in recipe:
            return dish, recipe[ingredient]
    return None, None


# ═══════════════════════════════════════════════════════════════
# 3. SUPPLIERS  (shared across every location)
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


SUPPLIERS: Dict[str, List[Supplier]] = {
    "carrot": [
        Supplier("FarmCo", 0.90, 2.0, 0.95, delivery_fee=5.0),
        Supplier("BulkVeg", 0.50, 4.0, 0.80, delivery_fee=15.0, free_shipping_at=80),
        Supplier("LocalMarket", 1.10, 1.0, 0.92, delivery_fee=0.0),
        Supplier("FreshDirect", 0.75, 3.0, 0.88, delivery_fee=8.0, free_shipping_at=60),
    ],
    "green_beans": [
        Supplier("FarmCo", 1.10, 2.0, 0.95, delivery_fee=5.0),
        Supplier("GreenGrocer", 0.95, 3.0, 0.85, delivery_fee=6.0),
        Supplier("BulkVeg", 0.70, 5.0, 0.78, delivery_fee=15.0),
        Supplier("FreshDirect", 1.05, 2.5, 0.90, delivery_fee=8.0),
    ],
    "chicken": [
        Supplier("MeatDirect", 4.20, 1.0, 0.98, delivery_fee=10.0),
        Supplier("FarmCo", 4.50, 2.0, 0.95, delivery_fee=5.0),
        Supplier("CityButcher", 4.80, 0.5, 0.97, delivery_fee=0.0),
        Supplier("BulkMeat", 3.90, 3.0, 0.82, delivery_fee=20.0, free_shipping_at=15),
    ],
    "oil": [
        # OilPro sells only by the case of 12 bottles -- demonstrates case rounding
        Supplier("BulkVeg", 3.60, 4.0, 0.80, delivery_fee=15.0),
        Supplier("OilPro", 4.10, 2.0, 0.93, delivery_fee=5.0, case_size=12),
        Supplier("FreshDirect", 3.90, 3.0, 0.88, delivery_fee=8.0),
        Supplier("LocalMarket", 4.50, 1.0, 0.91, delivery_fee=0.0),
    ],
    "onion": [
        Supplier("FarmCo", 0.40, 2.0, 0.95, delivery_fee=5.0),
        Supplier("BulkVeg", 0.25, 4.0, 0.80, delivery_fee=15.0),
        Supplier("LocalMarket", 0.55, 1.0, 0.90, delivery_fee=0.0),
        Supplier("FreshDirect", 0.35, 3.0, 0.87, delivery_fee=8.0),
    ],
    "garlic": [
        Supplier("FarmCo", 2.20, 2.0, 0.95, delivery_fee=5.0),
        Supplier("BulkVeg", 1.60, 4.0, 0.80, delivery_fee=15.0),
        Supplier("LocalMarket", 2.80, 1.0, 0.90, delivery_fee=0.0),
        Supplier("FreshDirect", 2.00, 3.0, 0.88, delivery_fee=8.0),
    ],
    "bread": [
        Supplier("BakeryFresh", 1.80, 1.0, 0.96, delivery_fee=0.0),
        Supplier("BulkBakery", 1.20, 2.0, 0.85, delivery_fee=12.0),
        Supplier("LocalMarket", 2.10, 0.5, 0.93, delivery_fee=0.0),
        Supplier("CityBread", 1.50, 1.5, 0.90, delivery_fee=6.0),
    ],
    "lettuce": [
        Supplier("FarmCo", 1.30, 2.0, 0.95, delivery_fee=5.0),
        Supplier("GreenGrocer", 1.10, 3.0, 0.85, delivery_fee=6.0),
        Supplier("LocalMarket", 1.60, 1.0, 0.92, delivery_fee=0.0),
        Supplier("FreshDirect", 1.25, 2.5, 0.89, delivery_fee=8.0),
    ],
}


def choose_supplier(ingredient: str, quantity: float, prefer_speed: bool = False,
                     tie_margin: float = 0.08) -> Supplier:
    """Cost-first supplier choice: cheapest REAL total cost wins; speed and
    reliability only break ties within `tie_margin` (default 8%)."""
    key = ingredient.lower().strip()
    candidates = SUPPLIERS.get(key, [])
    if not candidates:
        raise ValueError(f"No suppliers configured for '{ingredient}'")

    viable = [s for s in candidates if quantity >= s.min_order] or candidates
    costed = [(s, s.total_cost(quantity)) for s in viable]
    cheapest_cost = min(c for _, c in costed)

    margin = tie_margin * 1.5 if prefer_speed else tie_margin
    threshold = cheapest_cost * (1 + margin)
    price_competitive = [s for s, c in costed if c <= threshold]

    def tiebreak(s: Supplier) -> float:
        return s.reliability_score * 0.6 + (1.0 / max(s.delivery_days, 0.1)) * 0.4

    return max(price_competitive, key=tiebreak)


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


_order_counter = 0


def _next_id() -> str:
    global _order_counter
    _order_counter += 1
    return f"ORD-{_order_counter:05d}"


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


class Location:
    def __init__(self, name: str, max_capacity: int, weekly_budget: float,
                 max_days_to_stock_ahead: int = 3, order_cooldown_hours: float = 4.0):
        self.name = name
        self.inventory = Inventory()
        self.order_history: List[Order] = []
        self.notifications: List[str] = []

        # --- safety limits (edit per real restaurant) ---
        self.max_capacity = max_capacity
        self.max_days_to_stock_ahead = max_days_to_stock_ahead
        self.max_people_per_request = max_capacity * max_days_to_stock_ahead
        self.weekly_budget = weekly_budget
        self.spend_this_week = 0.0
        self.order_cooldown_hours = order_cooldown_hours
        self._last_order_time: Dict[str, datetime] = {}   # ingredient -> last order time

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
        print(f"  >> NOTIFICATION to {self.name} owner: {message}")

    # ---------- ordering ----------

    def place_order(self, ingredient: str, quantity: float, trigger: str,
                     force: bool = False, prefer_speed: bool = False) -> Order:
        if quantity <= 0:
            raise ValueError("Order quantity must be positive")

        if not force:
            self.check_duplicate(ingredient)

        supplier = choose_supplier(ingredient, quantity, prefer_speed=prefer_speed)
        qty = max(supplier.round_to_case(quantity), supplier.min_order)
        cost = round(supplier.total_cost(qty), 2)

        self.check_budget(cost)  # raises before anything is committed

        spoil_note = self.spoilage_warning(ingredient, qty)

        order = Order(
            order_id=_next_id(),
            ingredient=ingredient,
            quantity=qty,
            supplier=supplier,
            trigger=trigger,
            unit_price=supplier.price_per_unit,
            total_cost=cost,
            expected_arrival=datetime.now() + timedelta(days=supplier.delivery_days),
        )
        self.order_history.append(order)
        self.spend_this_week += cost
        self._last_order_time[ingredient] = datetime.now()

        fee_charged = 0.0 if (supplier.free_shipping_at and qty >= supplier.free_shipping_at) else supplier.delivery_fee
        free_shipping_hit = fee_charged == 0.0 and supplier.delivery_fee > 0.0
        fee_line = f"${fee_charged:.2f}" + ("  (FREE SHIPPING)" if free_shipping_hit else "")

        print(
            f"\n[ORDER PLACED - PENDING] {order.order_id}\n"
            f"  Ingredient    : {qty:.2f} of {ingredient}\n"
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

        self.notify(
            f"Ordered {qty:.2f} {ingredient} from {supplier.name} (${cost:.2f}), "
            f"arriving ~{order.expected_arrival:%b %d}. [{order.order_id}]"
            + (f" {spoil_note}" if spoil_note else "")
        )
        return order

    def receive_order(self, order_id: str) -> str:
        order = next((o for o in self.order_history if o.order_id == order_id), None)
        if not order:
            return f"No order with id {order_id}."
        if order.status != "pending":
            return f"Order {order_id} is already {order.status}, nothing to receive."
        order.status = "received"
        self.inventory.add_stock(order.ingredient, order.quantity)
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
        self.notify(f"CANCELLED order {order_id} ({order.quantity:.2f} {order.ingredient}, ${order.total_cost:.2f} refunded from weekly spend).")
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

        print(f"\n--- {self.name}: punching sale: {servings} x {dish} ---")
        for ingredient, per_serving in recipe.items():
            amount = per_serving * servings
            new_stock = self.inventory.deduct(ingredient, amount)
            item = self.inventory.get(ingredient)
            print(f"  - {amount:.2f} {item.unit} {ingredient}  ->  stock now {new_stock:.2f}")

            if self.inventory.needs_reorder(ingredient):
                print(f"  ! {ingredient} below reorder point ({item.reorder_point})")
                try:
                    self.place_order(ingredient, item.reorder_qty, trigger="auto-low-stock")
                except (BudgetExceededError, DuplicateOrderError) as e:
                    print(f"  AUTO-ORDER SKIPPED: {e}")


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

INGREDIENT_ALIASES = {
    "carrots": "carrot", "carrot": "carrot",
    "green beans": "green_beans", "beans": "green_beans",
    "chicken": "chicken", "oil": "oil",
    "onion": "onion", "onions": "onion",
    "garlic": "garlic", "bread": "bread", "lettuce": "lettuce",
}


def _normalize_item(raw: str) -> Optional[str]:
    text = raw.strip().lower()
    if text in INGREDIENT_ALIASES:
        return INGREDIENT_ALIASES[text]
    if text.endswith("s") and text[:-1] in INGREDIENT_ALIASES:
        return INGREDIENT_ALIASES[text[:-1]]
    return None


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
            people = int(groups["people"])
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
                location.place_order(ingredient, qty_to_order, trigger="human-request")
            except (BudgetExceededError, DuplicateOrderError) as e:
                return f"ORDER BLOCKED: {e}"
            return f"Needed {needed:.2f} {item.unit} of {ingredient} for {people} people. Had {current_stock:.2f}, ordered {qty_to_order:.2f} more."

        # Case 2: "order N X" or "order X"
        if "qty" in groups:
            qty = float(groups["qty"]) if groups.get("qty") else item.reorder_qty
            try:
                location.place_order(ingredient, qty, trigger="human-request")
            except (BudgetExceededError, DuplicateOrderError) as e:
                return f"ORDER BLOCKED: {e}"
            return f"Ordered {qty:.2f} {item.unit} of {ingredient}."

        # Case 3: "low on X"
        qty = item.reorder_qty
        try:
            location.place_order(ingredient, qty, trigger="human-request")
        except (BudgetExceededError, DuplicateOrderError) as e:
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
# 8. SAMPLE DATA + MULTI-LOCATION SETUP + CLI
# ═══════════════════════════════════════════════════════════════

def load_sample_location(name: str, max_capacity: int, weekly_budget: float) -> Location:
    loc = Location(name, max_capacity=max_capacity, weekly_budget=weekly_budget)
    inv = loc.inventory
    inv.add_ingredient("carrot", "unit", stock=80, reorder_point=20, reorder_qty=100,
                        shelf_life_days=14, daily_usage_estimate=8)
    inv.add_ingredient("oil", "L", stock=5, reorder_point=2, reorder_qty=10)  # doesn't spoil quickly
    inv.add_ingredient("green_beans", "kg", stock=3, reorder_point=2, reorder_qty=15,
                        shelf_life_days=5, daily_usage_estimate=1.5)
    inv.add_ingredient("chicken", "unit", stock=10, reorder_point=5, reorder_qty=20,
                        shelf_life_days=3, daily_usage_estimate=4)
    inv.add_ingredient("onion", "kg", stock=8, reorder_point=3, reorder_qty=10, shelf_life_days=30, daily_usage_estimate=1)
    inv.add_ingredient("garlic", "kg", stock=2, reorder_point=0.5, reorder_qty=2, shelf_life_days=45, daily_usage_estimate=0.2)
    inv.add_ingredient("bread", "loaf", stock=12, reorder_point=4, reorder_qty=15, shelf_life_days=4, daily_usage_estimate=3)
    inv.add_ingredient("lettuce", "kg", stock=4, reorder_point=1.5, reorder_qty=5, shelf_life_days=6, daily_usage_estimate=1)
    return loc


LOCATIONS: Dict[str, Location] = {
    "downtown": load_sample_location("Downtown", max_capacity=150, weekly_budget=1500),
    "uptown": load_sample_location("Uptown", max_capacity=80, weekly_budget=800),
}


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
    active_name = "downtown"
    print("=" * 70)
    print("  INVENTORY AI v3  --  multi-location, safety nets, real supplier math")
    print("=" * 70)
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
            servings = int(args[1]) if len(args) > 1 else 1
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
            dish, ingredient, base = args[0], args[1], float(args[2])
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
                    match = re.search(r"order\s+(?:(\d+(?:\.\d+)?)\s+)?([\w\s]+)", seg, re.IGNORECASE)
                    if match:
                        qty = float(match.group(1)) if match.group(1) else None
                        item_raw = match.group(2).strip()
                        ingredient = _normalize_item(item_raw)
                        if ingredient:
                            item = loc.inventory.get(ingredient)
                            qty = qty or item.reorder_qty
                            loc.place_order(ingredient, qty, trigger="human-request-forced", force=True)
                            continue
                except (BudgetExceededError, ValueError) as e:
                    print(f"ORDER BLOCKED: {e}")
            print(loc.inventory.status())

        else:
            print(handle_nl_request(loc, raw))
            print(loc.inventory.status())


if __name__ == "__main__":
    main()
