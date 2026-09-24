"""
Regions: each restaurant only uses the suppliers of its own region (Québec, Ontario...),
with the real regional catalog (estimated prices) from regional_catalog.py.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import ai_agent
import api
import db
import inventory_ai as ai
import regional_catalog
from conftest import _close_current_db


@pytest.fixture
def catalog_db(db_url, monkeypatch):
    monkeypatch.setenv("INVENTORY_CATALOG", "1")
    locs = ai.load_from_db(db_url)
    yield locs
    _close_current_db()


def _open_ontario(locations):
    db.create_location(ai.DB_SESSION, {"key": "toronto", "name": "Toronto", "region_code": "ON",
                                       "max_capacity": 100, "weekly_budget": 2000},
                       copy_ingredients_from=locations["downtown"].db_id)
    return ai.reload_from_db()["toronto"]


# ─────────────── catalog ───────────────

def test_catalog_has_many_suppliers_in_both_regions(catalog_db):
    names = {r: {s.name for offers in ai.SUPPLIERS.values() for s in offers if s.region == r} for r in ("QC", "ON")}
    assert len(names["QC"]) == len(regional_catalog.SUPPLIERS_BY_REGION["QC"]) >= 15
    assert len(names["ON"]) == len(regional_catalog.SUPPLIERS_BY_REGION["ON"]) >= 25
    assert {"Sysco Québec", "Colabor", "Mayrand Plus"} <= names["QC"]
    assert {"Flanagan Foodservice", "Gordon Food Service (GFS)"} <= names["ON"]


def test_every_catalog_price_is_marked_estimated(catalog_db):
    offers = [s for offers in ai.SUPPLIERS.values() for s in offers]
    assert offers and all(s.price_source == "estimate" and s.region in ("QC", "ON") for s in offers)


def test_catalog_prices_are_stable_between_runs():
    assert regional_catalog.build_offers() == regional_catalog.build_offers()


def test_specialists_only_carry_their_category(catalog_db):
    chenail = {p for p, offers in ai.SUPPLIERS.items() for s in offers if s.name == "Chenail Import-Export"}
    assert "carrot" in chenail and "salmon" not in chenail and "oil" not in chenail


# ─────────────── ordering stays in the region ───────────────

def test_quebec_restaurant_only_orders_from_quebec(catalog_db):
    downtown = catalog_db["downtown"]
    assert downtown.region == "QC"
    order = downtown.place_order("carrot", 100, trigger="test")
    assert order.supplier.region == "QC"
    plan = ai.plan_purchase({"carrot": 100}, "QC")          # best real cost incl. delivery fee and minimums
    assert order.total_cost == pytest.approx(plan.total) and order.supplier.name == plan.groups[0].supplier_name


def test_new_ontario_restaurant_orders_from_ontario(catalog_db):
    toronto = _open_ontario(catalog_db)
    assert toronto.region == "ON"
    assert toronto.inventory.get("carrot").stock == 0            # ingredient list copied, stock empty
    order = toronto.place_order("carrot", 100, trigger="test")
    assert order.supplier.region == "ON"


def test_supplier_for_every_region_is_used_everywhere(catalog_db):
    ai.SUPPLIERS["saffron"] = [ai.Supplier("Everywhere Inc", 5.0, 1, 0.9, region=None),
                               ai.Supplier("Ontario Only", 1.0, 1, 0.9, region="ON")]
    assert ai.choose_supplier("saffron", 1, region="QC").name == "Everywhere Inc"
    assert ai.choose_supplier("saffron", 1, region="ON").name == "Ontario Only"


def test_no_supplier_in_region_raises(catalog_db):
    ai.SUPPLIERS["saffron"] = [ai.Supplier("Ontario Only", 1.0, 1, 0.9, region="ON")]
    with pytest.raises(ValueError, match="region QC"):
        ai.choose_supplier("saffron", 1, region="QC")


def test_confirming_a_price(catalog_db, restart):
    offer = min(ai.suppliers_for("salmon", "QC"), key=lambda s: s.price_per_unit)
    row = ai.DB_SESSION.get(db.SupplierRow, offer.id)
    data = {c: getattr(row, c) for c in ("name", "ingredient_name", "delivery_days", "reliability_score",
                                         "delivery_fee", "free_shipping_at", "min_order", "case_size",
                                         "notes", "region_code")}
    db.upsert_supplier(ai.DB_SESSION, offer.id, {**data, "price_per_unit": 24.99})
    restart()
    saved = next(s for s in ai.SUPPLIERS["salmon"] if s.id == offer.id)
    assert (saved.price_per_unit, saved.price_source, saved.region) == (24.99, "confirmed", "QC")


# ─────────────── upgrading an old database file ───────────────

def test_old_database_is_upgraded(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE locations (id INTEGER PRIMARY KEY, key VARCHAR(50) UNIQUE, name VARCHAR(100),
            max_capacity INTEGER, weekly_budget FLOAT, max_days_to_stock_ahead INTEGER,
            order_cooldown_hours FLOAT, spend_this_week FLOAT);
        CREATE TABLE suppliers (id INTEGER PRIMARY KEY, name VARCHAR(100), ingredient_name VARCHAR(100),
            price_per_unit FLOAT, delivery_days FLOAT, reliability_score FLOAT, delivery_fee FLOAT,
            free_shipping_at FLOAT, min_order FLOAT, case_size FLOAT, notes TEXT);
        INSERT INTO locations VALUES (1, 'downtown', 'Downtown', 150, 1500, 3, 4.0, 42.0);
        INSERT INTO suppliers VALUES (1, 'FarmCo', 'carrot', 0.9, 2, 0.95, 5, NULL, 1, NULL, '');
    """)
    con.close()
    locs = ai.load_from_db(f"sqlite:///{path}", catalog=True)
    try:
        assert locs["downtown"].region == "QC" and locs["downtown"].spend_this_week == 42.0   # data kept
        farmco = next(s for s in ai.SUPPLIERS["carrot"] if s.name == "FarmCo")
        assert farmco.region is None and farmco.price_source == "confirmed"                  # old supplier kept
        assert len(ai.suppliers_for("carrot", "QC")) > 10                                    # catalog added
    finally:
        _close_current_db()


# ─────────────── bigger catalog, French words, fixing units ───────────────

@pytest.mark.parametrize("query, product", [
    ("mayo", "mayonnaise"), ("Coca-Cola", "coke"), ("coke", "coke"), ("frites", "french_fries"),
    ("fromage en grains", "cheese_curds"), ("mayonaise", "mayonnaise"), ("burger buns", "burger_buns"),
    ("huile d'olive", "olive_oil"), ("tomatoes", "tomato"),
])
def test_find_products_understands_staff_words(query, product):
    assert regional_catalog.find_products(query)[0] == product


def test_every_product_has_a_french_name():
    missing = [p for p in regional_catalog.PRODUCTS if p not in regional_catalog.FRENCH_NAMES]
    assert missing == []


@pytest.mark.parametrize("query, product", [
    ("miel", "honey"), ("Miel", "honey"), ("creme", "cream"), ("crème sure", "sour_cream"), ("oeufs", "eggs"),
    ("poulet", "chicken"), ("sirop d'erable", "maple_syrup"), ("pain a burger", "burger_buns"),
    ("tomates", "tomato"), ("fromage", "cheese"), ("saumon", "salmon"), ("pates", "pasta"),
])
def test_french_words_accents_optional(query, product):
    assert regional_catalog.find_products(query)[0] == product


def test_simple_mode_understands_french_catalog_words(catalog_db):
    assert ai._normalize_item("miel") == "honey"
    assert ai._normalize_item("carottes") == "carrot"
    assert ai._normalize_item("blablabla") is None       # no guessing in simple mode


def test_drinks_and_condiments_have_quebec_suppliers(catalog_db):
    coke = {s.name for s in ai.suppliers_for("coke", "QC")}
    assert "Coca-Cola Canada Bottling" in coke and "PepsiCo Canada" not in coke and "Sysco Québec" in coke
    assert "PepsiCo Canada" in {s.name for s in ai.suppliers_for("pepsi", "QC")}
    assert len(ai.suppliers_for("mayonnaise", "QC")) >= 10


def test_cheapest_is_not_always_the_same_supplier(catalog_db):
    winners = {ai.choose_supplier(p, 10, region="QC").name for p in regional_catalog.PRODUCTS}
    assert len(winners) >= 6


def test_sync_adds_new_products_and_never_touches_confirmed_prices(catalog_db):
    session = ai.DB_SESSION
    colabor_carrot = db.find_supplier_offer(session, "Colabor", "carrot", "QC")
    data = {c: getattr(colabor_carrot, c) for c in ("name", "ingredient_name", "delivery_days", "reliability_score",
                                                    "delivery_fee", "free_shipping_at", "min_order", "case_size",
                                                    "notes", "region_code")}
    db.upsert_supplier(session, colabor_carrot.id, {**data, "price_per_unit": 0.11})       # confirmed by invoice
    for row in session.scalars(select(db.SupplierRow).where(db.SupplierRow.ingredient_name == "coke")):
        session.delete(row)                                                                # "old" DB without coke
    session.commit()

    assert db.sync_catalog(session) > 0
    assert db.find_supplier_offer(session, "Coca-Cola Canada Bottling", "coke", "QC") is not None
    assert session.get(db.SupplierRow, colabor_carrot.id).price_per_unit == 0.11
    assert db.sync_catalog(session) == 0                                                   # nothing left to do


def test_order_blocked_when_units_dont_match_then_fixed(catalog_db):
    box = ai_agent.ToolBox("downtown")
    ai.DB_SESSION.add(db.IngredientRow(location_id=catalog_db["downtown"].db_id, name="mayonnaise", unit="tub",
                                       stock=2, reorder_point=1, reorder_qty=5, aliases="mayo"))
    ai.DB_SESSION.commit()
    ai.reload_from_db()
    out, err = box.run("place_order", {"ingredient": "mayo", "quantity": 5})
    assert err and "per 'L'" in out and "update_ingredient" in out            # tubs priced per L: refuse

    out, err = box.run("update_ingredient", {"name": "mayo", "unit": "L", "unit_factor": 4})
    assert not err and '"unit": "L"' in out and '"stock": 8' in out            # 2 tubs of 4 L = 8 L
    out, err = box.run("place_order", {"ingredient": "mayonnaise", "quantity": 20})
    assert not err and '"result": "ordered"' in out


def test_new_item_is_linked_to_catalog_directly(catalog_db):
    out, err = ai_agent.ToolBox("downtown").run("add_ingredient", {"name": "coca", "unit": "case", "aliases": "coca"})
    assert not err and '"name": "coke"' in out and '"suppliers_in_region": 0' not in out


def test_rename_to_catalog_name_links_suppliers(catalog_db):
    # an item created earlier under another name (like in an existing database)
    ai.DB_SESSION.add(db.IngredientRow(location_id=catalog_db["downtown"].db_id, name="coca", unit="case",
                                       stock=0, reorder_point=0, reorder_qty=1, aliases="coca"))
    ai.DB_SESSION.commit()
    ai.reload_from_db()
    box = ai_agent.ToolBox("downtown")
    assert "No supplier" in box.run("place_order", {"ingredient": "coca", "quantity": 2})[0]
    out, err = box.run("update_ingredient", {"name": "coca", "new_name": "coke"})
    assert not err and '"name": "coke"' in out
    out, err = box.run("place_order", {"ingredient": "coke", "quantity": 2})
    assert not err and '"quantity": 2' in out


def test_new_item_named_in_french_becomes_the_catalog_product(catalog_db):
    box = ai_agent.ToolBox("downtown")
    out, err = box.run("add_ingredient", {"name": "sauce_poutine", "unit": "kg", "aliases": "sauce poutine"})
    assert not err and '"name": "gravy_mix"' in out
    assert ai._normalize_item("sauce poutine") == "gravy_mix"
    out, err = box.run("order_items", {"items": [{"ingredient": "sauce poutine", "quantity": 3}]})
    assert not err and "gravy_mix" in out and '"not_ordered": []' in out


def test_unit_change_needs_factor_when_stock_exists(catalog_db):
    with pytest.raises(db.ConfigError, match="unit_factor"):
        db.update_ingredient(ai.DB_SESSION, catalog_db["downtown"].db_id, "carrot", unit="kg")


def test_compare_prices_finds_product_from_staff_word(catalog_db):
    box = ai_agent.ToolBox("downtown")
    out, err = box.run("compare_prices", {"product": "mayo", "quantity": 8})         # exact French word
    assert not err and '"product": "mayonnaise"' in out
    out, err = box.run("compare_prices", {"product": "mayonaisse", "quantity": 8})   # typo -> fuzzy match
    assert not err and '"product": "mayonnaise"' in out and "matched the catalog product" in out


# ─────────────── API + AI ───────────────

@pytest.fixture
def client(db_url, monkeypatch):
    monkeypatch.setenv("INVENTORY_CATALOG", "1")
    with TestClient(api.app) as c:
        yield c
    _close_current_db()


def test_api_regions_and_price_comparison(client):
    regions = {r["code"]: r for r in client.get("/regions").json()}
    assert regions["QC"]["suppliers"] >= 15 and regions["QC"]["restaurants"] == ["downtown", "uptown"]
    assert regions["ON"]["restaurants"] == []

    products = {p["product"]: p for p in client.get("/catalog?region=QC").json()}
    assert {"salmon", "olive_oil", "steak", "carrot"} <= set(products)
    assert products["salmon"]["min_price"] <= products["salmon"]["avg_price"] <= products["salmon"]["max_price"]
    assert products["salmon"]["estimated"]

    labels = client.get("/labels").json()
    assert labels["honey"]["fr"] == "Miel" and "miel" in labels["honey"]["words"]
    assert client.get("/catalog/miel?region=QC").status_code == 200        # French word works in the API too

    offers = client.get("/catalog/olive oil?region=ON&quantity=10").json()
    assert [o["total_cost"] for o in offers] == sorted(o["total_cost"] for o in offers)
    assert all(o["estimated"] for o in offers)
    assert client.get("/catalog/unobtainium?region=QC").status_code == 404


def test_api_open_restaurant_in_ontario(client):
    r = client.put("/config/locations", json={"key": "Toronto", "name": "Toronto King St", "region_code": "on",
                                              "max_capacity": 120, "weekly_budget": 2500,
                                              "copy_ingredients_from": "downtown"})
    assert r.status_code == 200 and r.json()["region"] == "ON"
    order = client.post("/order?location=toronto", json={"ingredient": "carrot", "quantity": 100}).json()["order"]
    assert order["supplier"] in {s[0] for s in regional_catalog.SUPPLIERS_BY_REGION["ON"]}
    assert client.put("/config/locations", json={"key": "toronto", "name": "x", "region_code": "ON",
                                                 "max_capacity": 1, "weekly_budget": 1}).status_code == 400
    assert client.put("/config/locations", json={"key": "paris", "name": "Paris", "region_code": "FR",
                                                 "max_capacity": 1, "weekly_budget": 1}).status_code == 400
    ontario = client.get("/config/suppliers?region=ON&ingredient=carrot").json()
    assert ontario and all(s["region_code"] == "ON" for s in ontario)


def test_ai_compares_prices_in_its_region_and_uses_catalog_units(catalog_db):
    box = ai_agent.ToolBox("downtown")
    out, err = box.run("compare_prices", {"product": "salmon", "quantity": 3})
    assert not err and '"estimated": true' in out and '"catalog_unit": "kg"' in out
    assert "Flanagan" not in out                                    # an Ontario supplier
    out, _ = box.run("add_ingredient", {"name": "salmon", "unit": "fillet", "aliases": "salmon,saumon"})
    assert '"unit": "kg"' in out and "match the regional catalog" in out
    out, err = box.run("place_order", {"ingredient": "saumon", "quantity": 3})
    assert not err and '"result": "ordered"' in out
