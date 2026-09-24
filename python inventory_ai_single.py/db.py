"""
Inventory AI — database layer (SQLite via SQLAlchemy)
======================================================
Tables: locations, ingredients, suppliers, recipes (+ recipe_items),
        orders, sales, notifications

  • open_session(url)  -> creates the tables, seeds demo data if the DB is empty
  • Store              -> write-through saver used by Location: every action
                          (sale, order, receive, cancel) is committed immediately

Default database file: inventory.db next to this file.
Override with the INVENTORY_DB_URL environment variable (e.g. for tests).
"""

import os
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
    create_engine, event, func, select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory.db")


def default_db_url() -> str:
    return os.environ.get("INVENTORY_DB_URL", f"sqlite:///{DEFAULT_DB_PATH}")


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    # SQLite ignores foreign keys unless asked to enforce them
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# ═══════════════════════════════════════════════════════════════
# 1. TABLES
# ═══════════════════════════════════════════════════════════════

class Base(DeclarativeBase):
    pass


class RegionRow(Base):
    """A market (province): its own suppliers and prices. See regional_catalog.py."""
    __tablename__ = "regions"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)   # "QC", "ON"
    name: Mapped[str] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(3), default="CAD")


class LocationRow(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(50), unique=True)       # "downtown" -- used by `use <location>`
    name: Mapped[str] = mapped_column(String(100))
    region_code: Mapped[Optional[str]] = mapped_column(ForeignKey("regions.code"), nullable=True)
    max_capacity: Mapped[int] = mapped_column(Integer)
    weekly_budget: Mapped[float] = mapped_column(Float)
    max_days_to_stock_ahead: Mapped[int] = mapped_column(Integer, default=3)
    order_cooldown_hours: Mapped[float] = mapped_column(Float, default=4.0)
    spend_this_week: Mapped[float] = mapped_column(Float, default=0.0)

    ingredients: Mapped[List["IngredientRow"]] = relationship(
        back_populates="location", order_by="IngredientRow.id", cascade="all, delete-orphan")


class IngredientRow(Base):
    """Stock of one ingredient at one location."""
    __tablename__ = "ingredients"
    __table_args__ = (UniqueConstraint("location_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    name: Mapped[str] = mapped_column(String(100))
    unit: Mapped[str] = mapped_column(String(20))
    stock: Mapped[float] = mapped_column(Float)
    reorder_point: Mapped[float] = mapped_column(Float)
    reorder_qty: Mapped[float] = mapped_column(Float)
    min_order_qty: Mapped[float] = mapped_column(Float, default=1.0)
    shelf_life_days: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    daily_usage_estimate: Mapped[float] = mapped_column(Float, default=0.0)
    last_ordered: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    aliases: Mapped[str] = mapped_column(Text, default="")          # comma-separated, for natural language

    location: Mapped[LocationRow] = relationship(back_populates="ingredients")


class SupplierRow(Base):
    """One supplier offer for one ingredient (FarmCo carrots != FarmCo chicken)."""
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    ingredient_name: Mapped[str] = mapped_column(String(100), index=True)
    price_per_unit: Mapped[float] = mapped_column(Float)
    delivery_days: Mapped[float] = mapped_column(Float)
    reliability_score: Mapped[float] = mapped_column(Float)
    delivery_fee: Mapped[float] = mapped_column(Float, default=0.0)
    free_shipping_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    min_order: Mapped[float] = mapped_column(Float, default=1.0)
    case_size: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    region_code: Mapped[Optional[str]] = mapped_column(ForeignKey("regions.code"), nullable=True)  # None = all
    price_source: Mapped[str] = mapped_column(String(20), default="confirmed")   # "estimate" or "confirmed"
    price_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # date of the invoice / entry


class SupplierTermsRow(Base):
    """Delivery conditions of a supplier in a region, charged once per delivery."""
    __tablename__ = "supplier_terms"
    __table_args__ = (UniqueConstraint("name", "region_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    region_code: Mapped[Optional[str]] = mapped_column(ForeignKey("regions.code"), nullable=True)
    min_order_value: Mapped[float] = mapped_column(Float, default=0.0)        # $ of goods to get a delivery
    delivery_fee: Mapped[float] = mapped_column(Float, default=0.0)           # per delivery
    free_delivery_over: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_source: Mapped[str] = mapped_column(String(20), default="confirmed")  # "estimate" / "confirmed"


class PurchaseOrderRow(Base):
    """One delivery from one supplier grouping several order lines (delivery fee paid once)."""
    __tablename__ = "purchase_orders"

    po_id: Mapped[str] = mapped_column(String(20), primary_key=True)   # "PO-00001"
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    supplier_name: Mapped[str] = mapped_column(String(100))
    delivery_fee: Mapped[float] = mapped_column(Float, default=0.0)
    placed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class RecipeRow(Base):
    __tablename__ = "recipes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    aliases: Mapped[str] = mapped_column(Text, default="")          # comma-separated, e.g. "carrot soup,soup"

    items: Mapped[List["RecipeItemRow"]] = relationship(
        back_populates="recipe", order_by="RecipeItemRow.id", cascade="all, delete-orphan")


class RecipeItemRow(Base):
    __tablename__ = "recipe_items"
    __table_args__ = (UniqueConstraint("recipe_id", "ingredient_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"))
    ingredient_name: Mapped[str] = mapped_column(String(100))
    qty_per_serving: Mapped[float] = mapped_column(Float)

    recipe: Mapped[RecipeRow] = relationship(back_populates="items")


class OrderRow(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(20), primary_key=True)   # "ORD-00001"
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    ingredient: Mapped[str] = mapped_column(String(100))
    quantity: Mapped[float] = mapped_column(Float)
    trigger: Mapped[str] = mapped_column(String(50))
    unit_price: Mapped[float] = mapped_column(Float)
    total_cost: Mapped[float] = mapped_column(Float)
    placed_at: Mapped[datetime] = mapped_column(DateTime)
    expected_arrival: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="pending")    # pending / received / cancelled
    po_id: Mapped[Optional[str]] = mapped_column(ForeignKey("purchase_orders.po_id"), nullable=True)


class SaleRow(Base):
    __tablename__ = "sales"

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    recipe_name: Mapped[str] = mapped_column(String(100))
    servings: Mapped[int] = mapped_column(Integer)
    sold_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class NotificationRow(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    message: Mapped[str] = mapped_column(Text)                  # already timestamped, as shown in the CLI
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


def split_aliases(text: str) -> List[str]:
    return [a.strip().lower() for a in (text or "").split(",") if a.strip()]


# ═══════════════════════════════════════════════════════════════
# 2. SESSION + DEMO SEED (only runs on an empty database)
# ═══════════════════════════════════════════════════════════════

def open_session(db_url: Optional[str] = None, seed: bool = True, catalog: bool = True) -> Session:
    """catalog=True: real regional suppliers (estimated prices) from regional_catalog.py.
    catalog=False: the small fictional supplier set (FarmCo, BulkVeg...) used by the tests."""
    engine = create_engine(db_url or default_db_url())
    Base.metadata.create_all(engine)
    _migrate(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    if seed:
        if session.scalar(select(LocationRow.id).limit(1)) is None:
            seed_demo_data(session, catalog=catalog)
        else:
            _upgrade_to_regions(session, catalog=catalog)
            if catalog:
                sync_catalog(session)
    return session


def sync_catalog(session: Session) -> int:
    """Bring an existing database up to date with regional_catalog.py: add new regions,
    suppliers and products, refresh ESTIMATED prices. Confirmed prices (from the restaurant's
    invoices) are never touched. Returns the number of rows added or changed."""
    from regional_catalog import build_offers, build_terms
    _seed_regions(session)
    changes = 0
    for model, rows, key in (
        (SupplierRow, build_offers(), lambda r: (r["name"], r["ingredient_name"], r["region_code"])),
        (SupplierTermsRow, build_terms(), lambda r: (r["name"], r["region_code"])),
    ):
        columns = ("name", "ingredient_name", "region_code") if model is SupplierRow else ("name", "region_code")
        existing = {tuple(getattr(r, c) for c in columns): r for r in session.scalars(select(model))}
        for data in rows:
            row = existing.get(key(data))
            if row is None:
                session.add(model(**data))
                changes += 1
            elif row.price_source == "estimate":   # confirmed by the restaurant: never overwritten
                fields = {k: v for k, v in data.items() if getattr(row, k) != v}
                for k, v in fields.items():
                    setattr(row, k, v)
                changes += bool(fields)
    if changes:
        session.commit()
    return changes


def _migrate(engine):
    """Add columns introduced after the first version to an existing database file."""
    new_columns = {
        "locations": {"region_code": "VARCHAR(10) REFERENCES regions(code)"},
        "suppliers": {"region_code": "VARCHAR(10) REFERENCES regions(code)",
                      "price_source": "VARCHAR(20) NOT NULL DEFAULT 'confirmed'",
                      "price_updated_at": "DATETIME"},
        "orders": {"po_id": "VARCHAR(20) REFERENCES purchase_orders(po_id)"},
    }
    with engine.begin() as conn:
        for table, columns in new_columns.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _seed_regions(session: Session):
    from regional_catalog import REGIONS
    for code, name, currency, _ in REGIONS:
        if session.get(RegionRow, code) is None:
            session.add(RegionRow(code=code, name=name, currency=currency))
    session.flush()


def _seed_catalog(session: Session):
    from regional_catalog import build_offers, build_terms
    session.add_all(SupplierRow(**offer) for offer in build_offers())
    session.add_all(SupplierTermsRow(**terms) for terms in build_terms())


def _upgrade_to_regions(session: Session, catalog: bool):
    """A database created before regions existed: add them, put existing restaurants
    in Québec, and add the regional catalog once."""
    if session.scalar(select(RegionRow.code).limit(1)) is not None:
        return
    _seed_regions(session)
    for loc in session.scalars(select(LocationRow).where(LocationRow.region_code.is_(None))):
        loc.region_code = "QC"
    if catalog:
        _seed_catalog(session)
    session.commit()


DEMO_RECIPES = [
    # name, aliases, {ingredient: qty per serving}
    ("carrot_soup", "carrot soup,soup", {"carrot": 1.0, "oil": 0.05, "onion": 0.1, "bread": 0.05}),
    ("green_beans_side", "green beans,beans", {"green_beans": 0.15, "oil": 0.02, "garlic": 0.01}),
    ("roast_chicken", "roast chicken,chicken", {"chicken": 1.0, "oil": 0.03, "garlic": 0.02, "onion": 0.1}),
    ("chicken_sandwich", "chicken sandwich,sandwich", {"chicken": 0.3, "bread": 0.15, "oil": 0.01, "lettuce": 0.05}),
    ("veggie_burger", "veggie burger,burger",
     {"bread": 0.15, "lettuce": 0.08, "onion": 0.05, "oil": 0.02, "green_beans": 0.05}),
    ("garlic_bread", "garlic bread", {"bread": 0.2, "garlic": 0.03, "oil": 0.04}),
]

DEMO_SUPPLIERS = {
    # ingredient: [(name, price, delivery_days, reliability, extra kwargs)]
    "carrot": [
        ("FarmCo", 0.90, 2.0, 0.95, dict(delivery_fee=5.0)),
        ("BulkVeg", 0.50, 4.0, 0.80, dict(delivery_fee=15.0, free_shipping_at=80)),
        ("LocalMarket", 1.10, 1.0, 0.92, dict(delivery_fee=0.0)),
        ("FreshDirect", 0.75, 3.0, 0.88, dict(delivery_fee=8.0, free_shipping_at=60)),
    ],
    "green_beans": [
        ("FarmCo", 1.10, 2.0, 0.95, dict(delivery_fee=5.0)),
        ("GreenGrocer", 0.95, 3.0, 0.85, dict(delivery_fee=6.0)),
        ("BulkVeg", 0.70, 5.0, 0.78, dict(delivery_fee=15.0)),
        ("FreshDirect", 1.05, 2.5, 0.90, dict(delivery_fee=8.0)),
    ],
    "chicken": [
        ("MeatDirect", 4.20, 1.0, 0.98, dict(delivery_fee=10.0)),
        ("FarmCo", 4.50, 2.0, 0.95, dict(delivery_fee=5.0)),
        ("CityButcher", 4.80, 0.5, 0.97, dict(delivery_fee=0.0)),
        ("BulkMeat", 3.90, 3.0, 0.82, dict(delivery_fee=20.0, free_shipping_at=15)),
    ],
    "oil": [
        # OilPro sells only by the case of 12 bottles -- demonstrates case rounding
        ("BulkVeg", 3.60, 4.0, 0.80, dict(delivery_fee=15.0)),
        ("OilPro", 4.10, 2.0, 0.93, dict(delivery_fee=5.0, case_size=12)),
        ("FreshDirect", 3.90, 3.0, 0.88, dict(delivery_fee=8.0)),
        ("LocalMarket", 4.50, 1.0, 0.91, dict(delivery_fee=0.0)),
    ],
    "onion": [
        ("FarmCo", 0.40, 2.0, 0.95, dict(delivery_fee=5.0)),
        ("BulkVeg", 0.25, 4.0, 0.80, dict(delivery_fee=15.0)),
        ("LocalMarket", 0.55, 1.0, 0.90, dict(delivery_fee=0.0)),
        ("FreshDirect", 0.35, 3.0, 0.87, dict(delivery_fee=8.0)),
    ],
    "garlic": [
        ("FarmCo", 2.20, 2.0, 0.95, dict(delivery_fee=5.0)),
        ("BulkVeg", 1.60, 4.0, 0.80, dict(delivery_fee=15.0)),
        ("LocalMarket", 2.80, 1.0, 0.90, dict(delivery_fee=0.0)),
        ("FreshDirect", 2.00, 3.0, 0.88, dict(delivery_fee=8.0)),
    ],
    "bread": [
        ("BakeryFresh", 1.80, 1.0, 0.96, dict(delivery_fee=0.0)),
        ("BulkBakery", 1.20, 2.0, 0.85, dict(delivery_fee=12.0)),
        ("LocalMarket", 2.10, 0.5, 0.93, dict(delivery_fee=0.0)),
        ("CityBread", 1.50, 1.5, 0.90, dict(delivery_fee=6.0)),
    ],
    "lettuce": [
        ("FarmCo", 1.30, 2.0, 0.95, dict(delivery_fee=5.0)),
        ("GreenGrocer", 1.10, 3.0, 0.85, dict(delivery_fee=6.0)),
        ("LocalMarket", 1.60, 1.0, 0.92, dict(delivery_fee=0.0)),
        ("FreshDirect", 1.25, 2.5, 0.89, dict(delivery_fee=8.0)),
    ],
}

DEMO_INGREDIENTS = [
    # name, unit, stock, reorder_point, reorder_qty, shelf_life_days, daily_usage_estimate, aliases
    ("carrot", "unit", 80, 20, 100, 14, 8, "carrots,carrot"),
    ("oil", "L", 5, 2, 10, None, 0.0, "oil"),           # doesn't spoil quickly
    ("green_beans", "kg", 3, 2, 15, 5, 1.5, "green beans,beans"),
    ("chicken", "unit", 10, 5, 20, 3, 4, "chicken"),
    ("onion", "kg", 8, 3, 10, 30, 1, "onion,onions"),
    ("garlic", "kg", 2, 0.5, 2, 45, 0.2, "garlic"),
    ("bread", "loaf", 12, 4, 15, 4, 3, "bread"),
    ("lettuce", "kg", 4, 1.5, 5, 6, 1, "lettuce"),
]

DEMO_LOCATIONS = [
    # key, name, max_capacity, weekly_budget
    ("downtown", "Downtown", 150, 1500),
    ("uptown", "Uptown", 80, 800),
]


def seed_demo_data(session: Session, catalog: bool = True):
    _seed_regions(session)

    for name, aliases, items in DEMO_RECIPES:
        recipe = RecipeRow(name=name, aliases=aliases)
        recipe.items = [RecipeItemRow(ingredient_name=i, qty_per_serving=q) for i, q in items.items()]
        session.add(recipe)

    if catalog:
        _seed_catalog(session)
    else:
        for ingredient, offers in DEMO_SUPPLIERS.items():
            for name, price, days, reliability, extra in offers:
                session.add(SupplierRow(name=name, ingredient_name=ingredient, price_per_unit=price,
                                        delivery_days=days, reliability_score=reliability, **extra))

    for key, name, capacity, budget in DEMO_LOCATIONS:
        loc = LocationRow(key=key, name=name, max_capacity=capacity, weekly_budget=budget, region_code="QC")
        for (ing, unit, stock, rp, rq, shelf, usage, aliases) in DEMO_INGREDIENTS:
            loc.ingredients.append(IngredientRow(
                name=ing, unit=unit, stock=stock, reorder_point=rp, reorder_qty=rq,
                shelf_life_days=shelf, daily_usage_estimate=usage, aliases=aliases,
            ))
        session.add(loc)

    session.commit()


# ═══════════════════════════════════════════════════════════════
# 3. STORE  (write-through: each call commits immediately)
# ═══════════════════════════════════════════════════════════════

class Store:
    def __init__(self, session: Session):
        self.session = session

    def _commit(self):
        try:
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

    def save_location(self, loc):
        row = self.session.get(LocationRow, loc.db_id)
        row.spend_this_week = loc.spend_this_week
        self._commit()

    def save_ingredient(self, loc, item):
        row = self.session.scalar(select(IngredientRow).where(
            IngredientRow.location_id == loc.db_id, IngredientRow.name == item.name))
        if row is None:
            row = IngredientRow(location_id=loc.db_id, name=item.name)
            self.session.add(row)
        row.unit = item.unit
        row.stock = item.stock
        row.reorder_point = item.reorder_point
        row.reorder_qty = item.reorder_qty
        row.min_order_qty = item.min_order_qty
        row.shelf_life_days = item.shelf_life_days
        row.daily_usage_estimate = item.daily_usage_estimate
        row.last_ordered = item.last_ordered
        self._commit()

    def save_order(self, loc, order):
        row = self.session.get(OrderRow, order.order_id)
        if row is None:
            row = OrderRow(order_id=order.order_id, location_id=loc.db_id)
            self.session.add(row)
        row.supplier_id = order.supplier.id
        row.ingredient = order.ingredient
        row.quantity = order.quantity
        row.trigger = order.trigger
        row.unit_price = order.unit_price
        row.total_cost = order.total_cost
        row.placed_at = order.placed_at
        row.expected_arrival = order.expected_arrival
        row.status = order.status
        row.po_id = order.po_id
        self._commit()

    def save_purchase_order(self, loc, po):
        if self.session.get(PurchaseOrderRow, po.po_id) is None:
            self.session.add(PurchaseOrderRow(po_id=po.po_id, location_id=loc.db_id, supplier_name=po.supplier_name,
                                              delivery_fee=po.delivery_fee, placed_at=po.placed_at))
            self._commit()

    def save_sale(self, loc, recipe_name: str, servings: int):
        self.session.add(SaleRow(location_id=loc.db_id, recipe_name=recipe_name, servings=servings))
        self._commit()

    def save_notification(self, loc, message: str):
        self.session.add(NotificationRow(location_id=loc.db_id, message=message))
        self._commit()


# ═══════════════════════════════════════════════════════════════
# 4. CONFIG  (admin edits from the web UI -- reload the app afterwards)
# ═══════════════════════════════════════════════════════════════

class ConfigError(Exception):
    pass


def norm_name(name: str) -> str:
    """'Green Beans ' -> 'green_beans' (the naming used by recipes and suppliers)."""
    return "_".join(name.lower().split())


def _commit(session: Session):
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


def upsert_ingredient(session: Session, location_id: int, data: dict) -> IngredientRow:
    name = norm_name(data["name"])
    if not name:
        raise ConfigError("Ingredient name is required.")
    row = session.scalar(select(IngredientRow).where(
        IngredientRow.location_id == location_id, IngredientRow.name == name))
    if row is None:
        row = IngredientRow(location_id=location_id, name=name)
        session.add(row)
    for field in ("unit", "stock", "reorder_point", "reorder_qty", "min_order_qty",
                  "shelf_life_days", "daily_usage_estimate"):
        setattr(row, field, data[field])
    row.aliases = data.get("aliases") or name.replace("_", " ")
    _commit(session)
    return row


def update_ingredient(session: Session, location_id: int, name: str, new_name: Optional[str] = None,
                      unit: Optional[str] = None, unit_factor: Optional[float] = None,
                      aliases: Optional[str] = None) -> IngredientRow:
    """Rename an ingredient and/or change its unit. unit_factor = how many NEW units are in
    one OLD unit (e.g. tub -> L with a 4 L tub: 4); stock, thresholds and the recipes'
    quantities are converted with it."""
    row = session.scalar(select(IngredientRow).where(
        IngredientRow.location_id == location_id, IngredientRow.name == norm_name(name)))
    if row is None:
        raise ConfigError(f"No ingredient '{name}' at this restaurant.")
    shared = session.scalar(select(func.count()).select_from(IngredientRow).where(
        IngredientRow.name == row.name, IngredientRow.location_id != location_id))
    recipe_lines = list(session.scalars(select(RecipeItemRow).where(RecipeItemRow.ingredient_name == row.name)))

    if unit and unit.strip() != row.unit:
        if unit_factor is None and (row.stock or recipe_lines):
            raise ConfigError(f"To change '{row.name}' from '{row.unit}' to '{unit}', give unit_factor: "
                              f"how many {unit} are in one {row.unit}.")
        if recipe_lines and shared:
            raise ConfigError(f"'{row.name}' is used by recipes shared with other restaurants: "
                              f"change its unit in every restaurant from the Configuration screen.")
        factor = unit_factor or 1.0
        if factor <= 0:
            raise ConfigError("unit_factor must be greater than 0.")
        for field in ("stock", "reorder_point", "reorder_qty", "min_order_qty", "daily_usage_estimate"):
            setattr(row, field, (getattr(row, field) or 0) * factor)
        for line in recipe_lines:
            line.qty_per_serving *= factor
        row.unit = unit.strip()

    if new_name and norm_name(new_name) != row.name:
        new = norm_name(new_name)
        if session.scalar(select(IngredientRow.id).where(IngredientRow.location_id == location_id,
                                                         IngredientRow.name == new)) is not None:
            raise ConfigError(f"'{new}' already exists at this restaurant.")
        if session.scalar(select(OrderRow.order_id).where(OrderRow.location_id == location_id,
                                                          OrderRow.ingredient == row.name).limit(1)):
            raise ConfigError(f"'{row.name}' already has orders: it can't be renamed (history would break).")
        if recipe_lines and shared:
            raise ConfigError(f"'{row.name}' is used by recipes shared with other restaurants: can't rename it here.")
        for line in recipe_lines:
            line.ingredient_name = new
        row.name = new

    if aliases is not None:
        row.aliases = aliases
    _commit(session)
    return row


def delete_ingredient(session: Session, location_id: int, name: str):
    row = session.scalar(select(IngredientRow).where(
        IngredientRow.location_id == location_id, IngredientRow.name == norm_name(name)))
    if row is None:
        raise ConfigError(f"No ingredient '{name}' at this location.")
    session.delete(row)
    _commit(session)


def upsert_supplier(session: Session, supplier_id: Optional[int], data: dict) -> SupplierRow:
    if supplier_id is None:
        row = SupplierRow()
        session.add(row)
    else:
        row = session.get(SupplierRow, supplier_id)
        if row is None:
            raise ConfigError(f"No supplier with id {supplier_id}.")
    if not data["name"].strip() or not norm_name(data["ingredient_name"]):
        raise ConfigError("Supplier name and ingredient are required.")
    row.name = data["name"].strip()
    row.ingredient_name = norm_name(data["ingredient_name"])
    for field in ("price_per_unit", "delivery_days", "reliability_score", "delivery_fee",
                  "free_shipping_at", "min_order", "case_size"):
        setattr(row, field, data[field])
    row.notes = data.get("notes") or ""
    region = data.get("region_code") or None
    if region is not None and session.get(RegionRow, region) is None:
        raise ConfigError(f"Unknown region '{region}'.")
    row.region_code = region
    row.price_source = "confirmed"   # entered by the restaurant = a real price
    row.price_updated_at = data.get("price_updated_at") or datetime.now()
    _commit(session)
    return row


def upsert_supplier_terms(session: Session, name: str, region_code: Optional[str], min_order_value: float,
                          delivery_fee: float, free_delivery_over: Optional[float]) -> SupplierTermsRow:
    """Delivery conditions entered by the restaurant (confirmed): minimum order, fee, free delivery."""
    if min_order_value < 0 or delivery_fee < 0 or (free_delivery_over is not None and free_delivery_over < 0):
        raise ConfigError("Amounts can't be negative.")
    wanted = _plain(name)
    row = next((r for r in session.scalars(select(SupplierTermsRow).where(SupplierTermsRow.region_code == region_code))
                if _plain(r.name) == wanted), None)
    if row is None:
        known = {r.name for r in session.scalars(select(SupplierRow)) if _plain(r.name) == wanted}
        row = SupplierTermsRow(name=known.pop() if known else name.strip(), region_code=region_code)
        session.add(row)
    row.min_order_value = min_order_value
    row.delivery_fee = delivery_fee
    row.free_delivery_over = free_delivery_over
    row.price_source = "confirmed"
    _commit(session)
    return row


def find_supplier_offer(session: Session, supplier_name: str, ingredient: str,
                        region: Optional[str]) -> Optional[SupplierRow]:
    """Existing offer of this supplier for this ingredient in this region (name match ignores case/accents)."""
    wanted = _plain(supplier_name)
    rows = session.scalars(select(SupplierRow).where(SupplierRow.ingredient_name == norm_name(ingredient)))
    for row in rows:
        if _plain(row.name) == wanted and row.region_code in (region, None):
            return row
    return None


def _plain(text: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(c for c in text.lower() if c.isalnum())


def create_restaurant_db(session: Session, key: str, name: str, region_code: str,
                         max_capacity: int, weekly_budget: float) -> LocationRow:
    """A REAL restaurant: regions + regional catalog + one restaurant, and nothing else
    (no demo ingredients, recipes or fake stock). Only on an empty database."""
    if session.scalar(select(LocationRow.id).limit(1)) is not None:
        raise ConfigError("This database already has restaurants.")
    _seed_regions(session)
    _seed_catalog(session)
    session.commit()
    return create_location(session, {"key": key, "name": name, "region_code": region_code,
                                     "max_capacity": max_capacity, "weekly_budget": weekly_budget})


def create_location(session: Session, data: dict, copy_ingredients_from: Optional[int] = None) -> LocationRow:
    """Open a new restaurant in a region, optionally with the same ingredient list
    (stock 0) as an existing one."""
    key = norm_name(data["key"])
    if not key or not data["name"].strip():
        raise ConfigError("A restaurant needs a key and a name.")
    if session.scalar(select(LocationRow.id).where(LocationRow.key == key)) is not None:
        raise ConfigError(f"A restaurant with key '{key}' already exists.")
    if session.get(RegionRow, data["region_code"]) is None:
        raise ConfigError(f"Unknown region '{data['region_code']}'.")
    row = LocationRow(key=key, name=data["name"].strip(), region_code=data["region_code"],
                      max_capacity=data["max_capacity"], weekly_budget=data["weekly_budget"],
                      max_days_to_stock_ahead=data.get("max_days_to_stock_ahead", 3),
                      order_cooldown_hours=data.get("order_cooldown_hours", 4.0))
    if copy_ingredients_from is not None:
        for i in session.scalars(select(IngredientRow).where(IngredientRow.location_id == copy_ingredients_from)):
            row.ingredients.append(IngredientRow(
                name=i.name, unit=i.unit, stock=0, reorder_point=i.reorder_point, reorder_qty=i.reorder_qty,
                min_order_qty=i.min_order_qty, shelf_life_days=i.shelf_life_days,
                daily_usage_estimate=i.daily_usage_estimate, aliases=i.aliases))
    session.add(row)
    _commit(session)
    return row


def delete_supplier(session: Session, supplier_id: int):
    row = session.get(SupplierRow, supplier_id)
    if row is None:
        raise ConfigError(f"No supplier with id {supplier_id}.")
    if session.scalar(select(OrderRow.order_id).where(OrderRow.supplier_id == supplier_id).limit(1)):
        raise ConfigError(f"{row.name} ({row.ingredient_name}) has past orders and can't be deleted "
                          f"(order history would break). Raise its price instead to stop using it.")
    session.delete(row)
    _commit(session)


def upsert_recipe(session: Session, name: str, aliases: str, items: dict) -> RecipeRow:
    name = norm_name(name)
    if not name or not items:
        raise ConfigError("A recipe needs a name and at least one ingredient.")
    row = session.scalar(select(RecipeRow).where(RecipeRow.name == name))
    if row is None:
        row = RecipeRow(name=name)
        session.add(row)
    row.aliases = aliases or ""
    row.items.clear()
    session.flush()   # delete old lines first, so re-adding the same ingredient doesn't hit the unique constraint
    row.items = [RecipeItemRow(ingredient_name=norm_name(i), qty_per_serving=q) for i, q in items.items()]
    _commit(session)
    return row


def delete_recipe(session: Session, name: str):
    row = session.scalar(select(RecipeRow).where(RecipeRow.name == norm_name(name)))
    if row is None:
        raise ConfigError(f"No recipe '{name}'.")
    session.delete(row)
    _commit(session)
