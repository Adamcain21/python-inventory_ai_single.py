"""
API tests (FastAPI TestClient) -- each test runs against its own temporary DB.
"""

import pytest
from fastapi.testclient import TestClient

import api
import inventory_ai as ai
from conftest import _close_current_db


@pytest.fixture
def client(db_url):
    with TestClient(api.app) as c:   # runs the startup -> load_from_db(INVENTORY_DB_URL)
        yield c
    _close_current_db()


def test_stock_flags_low_items(client):
    r = client.get("/stock", params={"location": "uptown"})
    assert r.status_code == 200
    assert r.json()["name"] == "Uptown"
    assert not any(i["low"] for i in r.json()["items"])
    client.post("/sale", json={"dish": "soup", "servings": 70})
    low = {i["name"] for i in client.get("/stock").json()["items"] if i["low"]}
    assert "carrot" in low


def test_sale_returns_deductions_and_auto_orders(client):
    r = client.post("/sale", json={"dish": "soup", "servings": 70})
    assert r.status_code == 200
    body = r.json()
    assert body["dish"] == "carrot_soup"
    carrot = next(d for d in body["deducted"] if d["ingredient"] == "carrot")
    assert carrot["stock_after"] == pytest.approx(10)
    assert "carrot" in [o["ingredient"] for o in body["new_orders"]]


def test_sale_errors(client):
    assert client.post("/sale", json={"dish": "pizza"}).status_code == 404
    assert client.post("/sale", json={"dish": "soup", "servings": 0}).status_code == 422
    assert client.post("/sale?location=nowhere", json={"dish": "soup"}).status_code == 404


def test_order_duplicate_and_force(client):
    assert client.post("/order", json={"ingredient": "carrots", "quantity": 50}).status_code == 200
    assert client.post("/order", json={"ingredient": "carrots", "quantity": 50}).status_code == 409
    assert client.post("/order", json={"ingredient": "carrots", "quantity": 50, "force": True}).status_code == 200
    assert client.post("/order", json={"ingredient": "caviar"}).status_code == 404


def test_order_over_budget_is_409(client):
    r = client.post("/order?location=uptown", json={"ingredient": "chicken", "quantity": 9999})
    assert r.status_code == 409
    assert "budget" in r.json()["detail"]


def test_receive_and_cancel(client):
    first = client.post("/order", json={"ingredient": "carrot", "quantity": 50}).json()["order"]["order_id"]
    second = client.post("/order", json={"ingredient": "onion", "quantity": 10}).json()["order"]["order_id"]

    assert client.post(f"/receive/{first}").status_code == 200
    assert client.post(f"/receive/{first}").status_code == 409      # already received
    assert client.post(f"/cancel/{first}").status_code == 409       # can't cancel received
    assert client.post(f"/cancel/{second}").status_code == 200
    assert client.post("/cancel/ORD-99999").status_code == 404

    statuses = {o["order_id"]: o["status"] for o in client.get("/orders").json()}
    assert statuses == {first: "received", second: "cancelled"}
    assert [o["order_id"] for o in client.get("/orders?status=cancelled").json()] == [second]
    carrot = next(i for i in client.get("/stock").json()["items"] if i["name"] == "carrot")
    assert carrot["stock"] == pytest.approx(130)


def test_chat_uses_natural_language_logic(client):
    r = client.post("/chat?location=uptown", json={"message": "need carrots and chicken for 30 people"})
    assert r.status_code == 200
    assert "Enough carrot" in r.json()["reply"]
    assert [o["ingredient"] for o in r.json()["new_orders"]] == ["chicken"]
    assert "SAFETY STOP" in client.post("/chat", json={"message": "need chicken for 9999 people"}).json()["reply"]


def test_config_new_ingredient_supplier_recipe_end_to_end(client):
    r = client.put("/config/ingredients", json={
        "name": "Tomato", "unit": "kg", "stock": 5, "reorder_point": 2, "reorder_qty": 10,
        "aliases": "tomato,tomatoes"})
    assert r.status_code == 200 and r.json()["name"] == "tomato"

    r = client.post("/config/suppliers", json={
        "name": "SunFarm", "ingredient_name": "tomato", "price_per_unit": 2.0,
        "delivery_days": 1, "reliability_score": 0.9})
    assert r.status_code == 200
    supplier_id = r.json()["id"]

    r = client.put("/config/recipes", json={
        "name": "tomato salad", "aliases": "salad", "ingredients": {"tomato": 0.5, "oil": 0.02}})
    assert r.status_code == 200 and r.json()["name"] == "tomato_salad"

    # the running app sees the new data immediately
    sale = client.post("/sale", json={"dish": "salad", "servings": 8}).json()   # tomato 5 -> 1, below 2
    assert sale["new_orders"][0]["supplier"] == "SunFarm"
    # the AI understands the new alias; blocked only because the sale just auto-ordered tomatoes
    reply = client.post("/chat", json={"message": "order 3 tomatoes"}).json()["reply"]
    assert "ORDER BLOCKED" in reply and "tomato was already ordered" in reply

    # a supplier with past orders can't be deleted (it would break the history)
    assert client.delete(f"/config/suppliers/{supplier_id}").status_code == 400

    # and everything is in the DB: reload from disk and check
    ai.reload_from_db()
    assert "tomato_salad" in ai.RECIPES
    assert ai.LOCATIONS["downtown"].inventory.get("tomato").stock == pytest.approx(1)


def test_config_edit_and_delete(client):
    oil = next(s for s in client.get("/config/suppliers?ingredient=oil").json() if s["name"] == "OilPro")
    oil["price_per_unit"] = 0.10
    assert client.put(f"/config/suppliers/{oil['id']}", json=oil).status_code == 200
    assert ai.choose_supplier("oil", 10).name == "OilPro"                    # now by far the cheapest

    assert client.delete("/config/recipes/garlic_bread").status_code == 200
    assert "garlic_bread" not in [d["name"] for d in client.get("/dishes").json()]
    assert client.delete("/config/recipes/garlic_bread").status_code == 400

    assert client.delete("/config/ingredients/lettuce?location=uptown").status_code == 200
    names = [i["name"] for i in client.get("/stock?location=uptown").json()["items"]]
    assert "lettuce" not in names
    assert "lettuce" in [i["name"] for i in client.get("/stock").json()["items"]]   # downtown untouched


def test_web_ui_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()
