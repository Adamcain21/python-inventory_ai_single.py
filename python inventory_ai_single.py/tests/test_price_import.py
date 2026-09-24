"""
Real prices from invoices / price lists (price_import.py), access code, real-restaurant setup.
The AI is a scripted fake: we test what the app does with what the AI returns.
"""

import base64
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import ai_agent
import api
import db
import inventory_ai as ai
import price_import
from conftest import _close_current_db
from test_ai_agent import FakeClaude, says


def invoice(lines, supplier="SYSCO QUEBEC INC.", match="Sysco Québec", date="2026-09-20"):
    return says(json.dumps({"supplier_name": supplier, "supplier_match": match, "document_date": date,
                            "document_type": "invoice", "lines": lines}))


def line(ingredient, price, unit="unit", case=None, confidence="high", is_new=False, description=None):
    return {"description": description or ingredient.upper(), "ingredient": ingredient, "is_new": is_new,
            "unit": unit, "price_per_unit": price, "case_size": case, "confidence": confidence, "note": ""}


@pytest.fixture
def catalog_db(db_url, monkeypatch):
    monkeypatch.setenv("INVENTORY_CATALOG", "1")
    locs = ai.load_from_db(db_url)
    yield locs
    _close_current_db()


# ─────────────── reading the document ───────────────

def test_document_blocks_by_file_type():
    assert price_import._document_block("image/jpeg", b"x")["type"] == "image"
    assert price_import._document_block("application/pdf", b"x")["source"]["media_type"] == "application/pdf"
    assert "carrot;0.19" in price_import._document_block("text/csv", b"carrot;0.19")["text"]
    with pytest.raises(ValueError, match="Unsupported"):
        price_import._document_block("application/zip", b"x")


def test_extract_returns_preview_and_saves_nothing(catalog_db):
    claude = FakeClaude(invoice([line("carrot", 0.19, case=50), line("Salmon", 24.5, "kg", is_new=True)]))
    before = ai.DB_SESSION.scalar(select(func.count()).select_from(db.SupplierRow))
    preview = price_import.extract_prices("downtown", "image/jpeg", b"fake-photo", client=claude)

    assert preview["supplier_name"] == "Sysco Québec" and preview["known_supplier"]
    carrot, salmon = preview["lines"]
    assert not carrot["is_new"] and carrot["inventory_unit"] == "unit"
    assert carrot["current_price"] is not None and carrot["current_price_source"] == "estimate"
    assert salmon["ingredient"] == "salmon" and salmon["is_new"] and salmon["inventory_unit"] == "kg"
    assert ai.DB_SESSION.scalar(select(func.count()).select_from(db.SupplierRow)) == before

    request = claude.requests[0]
    content = request["messages"][0]["content"]
    assert content[0]["type"] == "image" and "Sysco Québec" in content[1]["text"]
    assert request["output_config"]["format"]["type"] == "json_schema"


def test_unknown_supplier_match_is_ignored(catalog_db):
    claude = FakeClaude(invoice([line("carrot", 0.2)], supplier="Ferme Tremblay", match="Made Up Supplier"))
    preview = price_import.extract_prices("downtown", "application/pdf", b"%PDF", client=claude)
    assert preview["supplier_name"] == "Ferme Tremblay" and not preview["known_supplier"]


# ─────────────── saving the checked lines ───────────────

def test_apply_confirms_existing_offer_with_invoice_date(catalog_db, restart):
    result = price_import.apply_prices("downtown", "Sysco Québec", "2026-09-20",
                                       [{"ingredient": "carrot", "unit": "unit", "price_per_unit": 0.05,
                                         "case_size": 50}])
    assert result["updated"] == ["carrot"] and result["created"] == []
    restart()
    sysco = next(s for s in ai.suppliers_for("carrot", "QC") if s.name == "Sysco Québec")
    assert (sysco.price_per_unit, sysco.price_source) == (0.05, "confirmed")
    assert sysco.price_updated_at == datetime(2026, 9, 20)
    # $5 of carrots is far below Sysco's delivery minimum: a small order goes to someone who delivers it...
    assert ai.LOCATIONS["downtown"].place_order("carrot", 100, trigger="test").supplier.name != "Sysco Québec"
    # ...but as soon as the order is big enough, the confirmed (cheapest) Sysco price wins
    assert ai.plan_purchase({"carrot": 10000}, "QC").groups[0].supplier_name == "Sysco Québec"


def test_apply_creates_new_supplier_and_new_ingredient(catalog_db):
    result = price_import.apply_prices("downtown", "Ferme Tremblay", "2026-09-21", [
        {"ingredient": "fiddleheads", "unit": "kg", "price_per_unit": 18.0, "add_new": True},
        {"ingredient": "carrot", "unit": "unit", "price_per_unit": 0.21},
    ])
    assert result["created"] == ["fiddleheads", "carrot"] and result["added_ingredients"] == ["fiddleheads"]
    downtown = ai.LOCATIONS["downtown"]
    assert downtown.inventory.get("fiddleheads").unit == "kg"
    tremblay = ai.suppliers_for("fiddleheads", "QC")
    assert [(s.name, s.region, s.price_source) for s in tremblay] == [("Ferme Tremblay", "QC", "confirmed")]
    assert ai.suppliers_for("fiddleheads", "ON") == []          # a Québec invoice doesn't feed Ontario


def test_apply_refuses_wrong_unit_and_unticked_new_items(catalog_db):
    result = price_import.apply_prices("downtown", "Sysco Québec", None, [
        {"ingredient": "carrot", "unit": "kg", "price_per_unit": 2.0},           # inventory counts carrots in "unit"
        {"ingredient": "truffle", "unit": "kg", "price_per_unit": 900.0},       # new, add_new not ticked
    ])
    assert result["updated"] == [] and result["created"] == [] and len(result["skipped"]) == 2
    assert ai.LOCATIONS["downtown"].inventory.get("truffle") is None


def test_future_or_bad_dates_are_clamped_to_today(catalog_db):
    future = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")
    for date in (future, "23/09/2026"):
        price_import.apply_prices("downtown", "Colabor", date,
                                  [{"ingredient": "carrot", "unit": "unit", "price_per_unit": 0.2}])
        colabor = next(s for s in ai.suppliers_for("carrot", "QC") if s.name == "Colabor")
        assert colabor.price_updated_at <= datetime.now()


def test_price_age():
    s = ai.Supplier("X", 1, 1, 0.9, price_source="confirmed", price_updated_at=datetime.now() - timedelta(days=45))
    assert price_import.price_age_days(s) == 45
    assert price_import.price_age_days(ai.Supplier("Y", 1, 1, 0.9, price_source="estimate")) is None


# ─────────────── API ───────────────

@pytest.fixture
def client(db_url, monkeypatch):
    monkeypatch.setenv("INVENTORY_CATALOG", "1")
    with TestClient(api.app) as c:
        yield c
    _close_current_db()


def _file(content=b"fake-photo", media_type="image/jpeg"):
    return {"filename": "facture.jpg", "media_type": media_type, "data_base64": base64.b64encode(content).decode()}


def test_api_import_needs_the_ai(client):
    r = client.post("/prices/import", json=_file())
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_api_import_then_apply(client, monkeypatch):
    monkeypatch.setattr(ai_agent, "is_configured", lambda: True)
    monkeypatch.setattr(ai_agent, "make_client", lambda: FakeClaude(invoice([line("carrot", 0.05, case=50)])))
    preview = client.post("/prices/import", json=_file()).json()
    assert preview["lines"][0]["ingredient"] == "carrot"

    body = {"supplier_name": preview["supplier_name"], "document_date": preview["document_date"],
            "lines": [{"ingredient": "carrot", "unit": "unit", "price_per_unit": 0.05, "case_size": 50}]}
    assert client.post("/prices/apply", json=body).json()["updated"] == ["carrot"]
    offers = client.get("/catalog/carrot?region=QC&quantity=100").json()
    assert offers[0]["supplier"] == "Sysco Québec" and not offers[0]["estimated"]
    assert offers[0]["price_age_days"] >= 0


def test_api_import_rejects_bad_files(client, monkeypatch):
    monkeypatch.setattr(ai_agent, "is_configured", lambda: True)
    assert client.post("/prices/import", json={**_file(), "data_base64": "not base64!!"}).status_code == 400
    assert client.post("/prices/import", json=_file(media_type="application/zip")).status_code == 400


def test_access_code(client, monkeypatch):
    monkeypatch.setenv("INVENTORY_ACCESS_CODE", "4242")
    assert client.get("/").status_code == 200                                  # the page itself loads
    assert client.get("/stock").status_code == 401
    assert client.get("/stock", headers={"X-Access-Code": "0000"}).status_code == 401
    assert client.get("/stock", headers={"X-Access-Code": "4242"}).status_code == 200


# ─────────────── real restaurant setup ───────────────

def test_real_restaurant_database_has_no_demo_data(tmp_path):
    session = db.open_session(f"sqlite:///{tmp_path / 'real.db'}", seed=False)
    try:
        db.create_restaurant_db(session, "chez_adam", "Chez Adam", "QC", 120, 3000)
        assert [l.key for l in session.scalars(select(db.LocationRow))] == ["chez_adam"]
        assert session.scalar(select(func.count()).select_from(db.IngredientRow)) == 0
        assert session.scalar(select(func.count()).select_from(db.RecipeRow)) == 0
        assert session.scalar(select(func.count()).select_from(db.SupplierRow)) > 500      # regional catalog
        with pytest.raises(db.ConfigError):
            db.create_restaurant_db(session, "again", "Again", "QC", 1, 1)
    finally:
        session.close()
        session.get_bind().dispose()
