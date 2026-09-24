"""
Smart ordering: several items -> one delivery per supplier, delivery fee paid once,
supplier minimum order respected, fewer deliveries when the cost is about the same.
"""

import pytest
from fastapi.testclient import TestClient

import ai_agent
import api
import db
import inventory_ai as ai
import regional_catalog
from conftest import _close_current_db


def supplier(name, price, fee=0.0, days=1.0, reliability=0.9):
    return ai.Supplier(name, price, days, reliability, delivery_fee=fee)


def terms(name, minimum=0.0, fee=0.0, free_over=None):
    ai.SUPPLIER_TERMS[(name, None)] = ai.SupplierTerms(name, None, min_order_value=minimum, delivery_fee=fee,
                                                       free_delivery_over=free_over)


@pytest.fixture
def market(locations):
    """Two products a/b. X and Y sell both, Z only sells 'a' (cheap). Every delivery costs $10.
    These made-up suppliers only exist in memory, so the restaurants don't save to the DB here."""
    ai.SUPPLIERS["a"] = [supplier("X", 1.20), supplier("Y", 0.90), supplier("Z", 0.50)]
    ai.SUPPLIERS["b"] = [supplier("X", 1.20), supplier("Y", 0.90)]
    for name in ("X", "Y", "Z"):
        terms(name, fee=10)
    for loc in locations.values():
        loc.store = None
    return locations


# ═══════════════════════════════════════════════════════════════
# 1. THE PLANNER
# ═══════════════════════════════════════════════════════════════

class TestPlanner:
    def test_delivery_fee_is_paid_once_so_grouping_wins(self, market):
        # item by item: a at Z (5+10) + b at Y (9+10) = 34 ; grouped at Y: 9+9+10 = 28
        plan = ai.plan_purchase({"a": 10, "b": 10})
        assert [g.supplier_name for g in plan.groups] == ["Y"]
        assert plan.total == pytest.approx(28.0)
        assert plan.groups[0].delivery_fee == 10

    def test_minimum_order_is_respected(self, market):
        terms("Y", minimum=50, fee=10)                     # Y's $18 of goods is below its $50 minimum
        plan = ai.plan_purchase({"a": 10, "b": 10})
        assert plan.feasible and "Y" not in [g.supplier_name for g in plan.groups]
        assert plan.total == pytest.approx(34.0)           # X both (24+10) or Z a + X b (15+22): both 34

    def test_no_feasible_plan_is_reported_not_hidden(self, market):
        for name in ("X", "Y", "Z"):
            terms(name, minimum=1000, fee=10)
        plan = ai.plan_purchase({"a": 10, "b": 10})
        assert not plan.feasible
        assert plan.groups[0].to_dict()["missing_for_minimum"] > 900

    def test_about_the_same_cost_means_fewer_deliveries(self, locations):
        ai.SUPPLIERS["a"] = [supplier("P", 1.00), supplier("Q", 0.45)]
        ai.SUPPLIERS["b"] = [supplier("P", 1.00), supplier("R", 0.95)]
        terms("P", fee=10)
        terms("Q", fee=15)
        terms("R", fee=0)
        # cheapest split Q a + R b = 4.5 + 15 + 9.5 = 29.0 ; one delivery from P = 30.0 (within 8%) -> P
        plan = ai.plan_purchase({"a": 10, "b": 10})
        assert [g.supplier_name for g in plan.groups] == ["P"]

    def test_much_cheaper_split_still_wins(self, locations):
        ai.SUPPLIERS["a"] = [supplier("P", 3.00), supplier("Q", 0.50)]
        ai.SUPPLIERS["b"] = [supplier("P", 3.00), supplier("R", 0.50)]
        terms("P", fee=10)
        terms("Q", fee=5)
        terms("R", fee=5)
        plan = ai.plan_purchase({"a": 10, "b": 10})        # split 20 vs single 70
        assert sorted(g.supplier_name for g in plan.groups) == ["Q", "R"]

    def test_free_delivery_threshold(self, market):
        terms("Y", fee=10, free_over=15)
        plan = ai.plan_purchase({"a": 10, "b": 10})        # $18 of goods >= $15: delivery free
        assert plan.total == pytest.approx(18.0) and plan.groups[0].delivery_fee == 0

    def test_single_item_same_choice_as_before(self, downtown):
        for name, item in downtown.inventory.items.items():
            plan = ai.plan_purchase({name: item.reorder_qty})
            chosen = ai.choose_supplier(name, item.reorder_qty)
            assert plan.groups[0].supplier_name == chosen.name, name
            assert plan.total == pytest.approx(round(chosen.total_cost(plan.groups[0].lines[0].quantity), 2))

    def test_unavailable_items_are_listed(self, market):
        plan = ai.plan_purchase({"a": 10, "unobtainium": 1})
        assert plan.unavailable == ["unobtainium"]


# ═══════════════════════════════════════════════════════════════
# 2. PLACING A BASKET = PURCHASE ORDERS
# ═══════════════════════════════════════════════════════════════

class TestBasket:
    def test_one_purchase_order_fee_counted_once(self, market, downtown):
        result = downtown.place_basket({"a": 10, "b": 10}, trigger="test")
        assert len(result.purchase_orders) == 1 and len(result.orders) == 2
        po = result.purchase_orders[0]
        assert po.supplier_name == "Y" and po.delivery_fee == 10
        assert {o.po_id for o in result.orders} == {po.po_id}
        assert sum(o.total_cost for o in result.orders) == pytest.approx(18.0)   # lines: goods only
        assert downtown.spend_this_week == pytest.approx(28.0)                   # + one delivery fee

    def test_purchase_orders_survive_restart(self, downtown, restart):
        downtown.place_basket({"oil": 10, "onion": 10}, trigger="test")          # LocalMarket, one delivery
        assert any(o.po_id for o in downtown.order_history)
        po_ids = {o.po_id for o in downtown.order_history}
        downtown = restart()["downtown"]
        assert {o.po_id for o in downtown.order_history} == po_ids
        assert set(downtown.purchase_orders) == {p for p in po_ids if p}

    def test_cancelling_the_whole_delivery_refunds_the_fee(self, market, downtown):
        result = downtown.place_basket({"a": 10, "b": 10}, trigger="test")
        first, second = result.orders
        downtown.cancel_order(first.order_id)
        assert downtown.spend_this_week == pytest.approx(9.0 + 10)  # fee still due: part of the delivery comes
        downtown.cancel_order(second.order_id)
        assert downtown.spend_this_week == pytest.approx(0.0)      # nothing comes: fee refunded

    def test_receive_whole_purchase_order(self, downtown):
        result = downtown.place_basket({"oil": 10, "onion": 10}, trigger="test")
        po = next(o.po_id for o in result.orders if o.po_id)
        message = downtown.receive_purchase_order(po)
        assert "received" in message
        assert downtown.inventory.get("oil").stock == pytest.approx(15)
        assert downtown.inventory.get("onion").stock == pytest.approx(18)
        assert "nothing left" in downtown.receive_purchase_order(po)

    def test_item_ordered_later_joins_todays_delivery(self, market, downtown):
        first = downtown.place_order("a", 10, trigger="test")          # Z: 5 + 10 fee
        assert first.supplier.name == "Z" and first.po_id is None
        ai.SUPPLIERS["c"] = [supplier("Z", 1.00), supplier("Y", 0.80)]
        result = downtown.place_basket({"c": 10}, trigger="test")       # Z today: +10, no new fee (vs Y 8+10)
        assert result.orders[0].supplier.name == "Z"
        assert result.orders[0].po_id == first.po_id == result.purchase_orders[0].po_id
        assert downtown.spend_this_week == pytest.approx(15 + 10)       # one delivery fee in total

    def test_joining_doesnt_happen_when_another_supplier_is_much_cheaper(self, market, downtown):
        downtown.place_order("a", 10, trigger="test")                   # Z today
        ai.SUPPLIERS["c"] = [supplier("Z", 5.00), supplier("Y", 0.50)]
        result = downtown.place_basket({"c": 10}, trigger="test")       # Z +50 vs Y 5+10: Y wins
        assert result.orders[0].supplier.name == "Y"

    def test_below_minimum_raises_when_not_partial(self, market, downtown):
        for name in ("X", "Y", "Z"):
            terms(name, minimum=1000)
        with pytest.raises(ai.MinimumOrderError, match="minimum|delivers from"):
            downtown.place_basket({"a": 10, "b": 10}, trigger="test")
        assert downtown.order_history == []

    def test_partial_orders_what_it_can(self, market, downtown):
        ai.SUPPLIERS["c"] = [supplier("W", 1.0)]
        terms("W", minimum=1000)
        result = downtown.place_basket({"a": 10, "b": 10, "c": 5, "unobtainium": 1}, trigger="test", partial=True)
        assert {o.ingredient for o in result.orders} == {"a", "b"}
        reasons = dict(result.skipped)
        assert "delivers from $1000.00" in reasons["c"] and "No suppliers" in reasons["unobtainium"]

    def test_force_below_minimum(self, market, downtown):
        for name in ("X", "Y", "Z"):
            terms(name, minimum=1000)
        result = downtown.place_basket({"a": 10}, trigger="test", allow_below_minimum=True)
        assert len(result.orders) == 1

    def test_budget_applies_to_the_whole_basket(self, market, uptown):
        uptown.spend_this_week = 790                              # $10 left, basket costs $28
        with pytest.raises(ai.BudgetExceededError):
            uptown.place_basket({"a": 10, "b": 10}, trigger="test")
        assert uptown.order_history == [] and uptown.spend_this_week == 790

    def test_single_place_order_respects_minimum(self, market, downtown):
        for name in ("X", "Y", "Z"):
            terms(name, minimum=1000)
        with pytest.raises(ai.MinimumOrderError):
            downtown.place_order("a", 10, trigger="test")


# ═══════════════════════════════════════════════════════════════
# 3. AUTOMATIC ORDERS AFTER A SALE
# ═══════════════════════════════════════════════════════════════

class TestAutoOrders:
    def test_low_items_after_a_sale_are_grouped(self, downtown):
        downtown.punch_sale("carrot_soup", 70)                     # carrot, oil and onion fall below their points
        lines = {o.ingredient: o for o in downtown.order_history}
        assert set(lines) == {"carrot", "oil", "onion"}
        assert lines["oil"].po_id and lines["oil"].po_id == lines["onion"].po_id   # same delivery (LocalMarket)

    def test_no_reorder_while_a_delivery_is_coming(self, downtown):
        downtown.punch_sale("carrot_soup", 70)
        count = len(downtown.order_history)
        downtown._last_order_time.clear()                          # even after the cooldown...
        downtown.punch_sale("carrot_soup", 5)
        assert len(downtown.order_history) == count                # ...nothing re-ordered: it's on the way

    def test_auto_order_waits_when_below_minimums(self, downtown):
        for offers in ai.SUPPLIERS.values():
            for s in offers:
                terms(s.name, minimum=5000)
        downtown.punch_sale("carrot_soup", 70)
        assert downtown.order_history == []
        needed, _ = downtown.suggested_order()
        assert set(needed) == {"carrot", "oil", "onion"}

    def test_suggested_order_lists_items_to_top_up(self, downtown):
        downtown.inventory.get("bread").stock = 5                  # reorder point 4: close, not yet low
        downtown.inventory.get("garlic").stock = 0.1               # below 0.5
        needed, could_add = downtown.suggested_order()
        assert "garlic" in needed and "bread" in could_add and "bread" not in needed


# ═══════════════════════════════════════════════════════════════
# 4. REAL CATALOG: estimated delivery terms
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def catalog_db(db_url, monkeypatch):
    monkeypatch.setenv("INVENTORY_CATALOG", "1")
    locs = ai.load_from_db(db_url)
    yield locs
    _close_current_db()


def test_catalog_has_estimated_terms_for_every_supplier(catalog_db):
    names = {(s[0], r) for r, sup in regional_catalog.SUPPLIERS_BY_REGION.items() for s in sup}
    assert names <= set(ai.SUPPLIER_TERMS)
    sysco = ai.terms_for("Sysco Québec", "QC")
    assert sysco.min_order_value > 0 and sysco.price_source == "estimate"
    assert all(s.delivery_fee == 0 for offers in ai.SUPPLIERS.values() for s in offers)   # fee is per delivery


def test_small_order_goes_to_a_supplier_that_delivers_it(catalog_db):
    plan = ai.plan_purchase({"honey": 3}, "QC")                    # ~$30: below every broadline minimum
    assert plan.feasible and plan.groups[0].min_order_value <= plan.groups[0].subtotal


def test_big_order_is_grouped_in_few_deliveries(catalog_db):
    items = {"chicken_breast": 20, "french_fries": 27.2, "cheese_curds": 9.2, "gravy_mix": 3, "burger_buns": 96,
             "ground_beef": 15, "ketchup": 8, "mayonnaise": 8, "coke": 5, "lettuce": 5, "tomato": 11}
    plan = ai.plan_purchase(items, "QC")
    assert plan.feasible and len(plan.groups) <= 3
    item_by_item = sum(ai.plan_purchase({k: v}, "QC").total for k, v in items.items())
    assert plan.total < item_by_item


def test_confirmed_terms_are_used_and_kept_by_sync(catalog_db):
    db.upsert_supplier_terms(ai.DB_SESSION, "sysco quebec", "QC", 150, 0, None)   # name typed loosely
    ai.reload_from_db()
    assert ai.terms_for("Sysco Québec", "QC").min_order_value == 150
    db.sync_catalog(ai.DB_SESSION)
    ai.reload_from_db()
    assert ai.terms_for("Sysco Québec", "QC").min_order_value == 150


# ═══════════════════════════════════════════════════════════════
# 5. API + AI
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def client(db_url):
    with TestClient(api.app) as c:
        yield c
    _close_current_db()


def test_api_basket_preview_order_and_receive(client):
    items = {"items": [{"ingredient": "oil", "quantity": 10}, {"ingredient": "onions", "quantity": 10}]}
    preview = client.post("/basket/preview", json=items).json()
    assert preview["deliveries"] == 1 and preview["groups"][0]["supplier"] == "LocalMarket"
    assert client.get("/orders").json() == []                                       # preview orders nothing

    placed = client.post("/basket", json=items).json()
    po = placed["purchase_orders"][0]["po_id"]
    assert {o["po_id"] for o in placed["orders"]} == {po}
    assert "received" in client.post(f"/receive-po/{po}").json()["message"]
    assert client.post("/receive-po/PO-99999").status_code == 404


def test_api_suggested_order(client):
    client.post("/sale", json={"dish": "soup", "servings": 70})      # auto-orders the low items
    assert client.get("/suggested-order").json()["needed"] == {}     # everything is on the way
    client.put("/config/ingredients", json={"name": "garlic", "unit": "kg", "stock": 0.1, "reorder_point": 0.5,
                                            "reorder_qty": 2, "aliases": "garlic"})
    suggestion = client.get("/suggested-order").json()
    assert "garlic" in suggestion["needed"] and suggestion["plan"]["deliveries"] >= 1
    placed = client.post("/suggested-order/place").json()
    assert [o["ingredient"] for o in placed["orders"]] == ["garlic"]


def test_api_terms_and_minimum_error(client):
    for name in ("FarmCo", "BulkVeg", "LocalMarket", "FreshDirect"):
        assert client.put("/config/supplier-terms", json={"supplier": name, "min_order_value": 5000}).status_code == 200
    r = client.post("/order", json={"ingredient": "carrot", "quantity": 50})
    assert r.status_code == 409 and "delivers from" in r.json()["detail"]
    terms_list = client.get("/config/supplier-terms").json()
    assert {t["supplier"] for t in terms_list if not t["estimated"]} >= {"FarmCo", "LocalMarket"}


def test_simple_chat_reports_minimum_instead_of_crashing(client):
    for name in ("FarmCo", "BulkVeg", "LocalMarket", "FreshDirect"):
        client.put("/config/supplier-terms", json={"supplier": name, "min_order_value": 5000})
    reply = client.post("/chat", json={"message": "order 50 carrots"}).json()["reply"]
    assert "ORDER BLOCKED" in reply


def test_ai_order_items_groups_deliveries(downtown):
    box = ai_agent.ToolBox("downtown")
    out, err = box.run("order_items", {"items": [{"ingredient": "oil", "quantity": 10},
                                                 {"ingredient": "onion", "quantity": 10},
                                                 {"ingredient": "carrot", "quantity": 100}]})
    assert not err and '"ordered_deliveries"' in out
    history = ai.LOCATIONS["downtown"].order_history
    # BulkVeg for all three ($103.50) is within 8% of the cheapest split ($100.50): one delivery instead of two
    assert len(history) == 3 and len({o.po_id for o in history}) == 1 and history[0].po_id

    out, err = box.run("preview_order", {"items": [{"ingredient": "garlic"}]})
    assert not err and '"deliveries": 1' in out
    out, err = box.run("set_supplier_terms", {"supplier": "FarmCo", "min_order_value": 250, "delivery_fee": 5})
    assert not err and ai.terms_for("FarmCo", "QC").min_order_value == 250     # saved for the restaurant's region
    out, err = box.run("suggest_order", {})
    assert not err and '"needed"' in out
