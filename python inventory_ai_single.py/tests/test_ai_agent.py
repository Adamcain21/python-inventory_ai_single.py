"""
AI assistant tests. No API key, no network: a scripted fake Claude client plays
the model's side, so we test the tools, the agent loop and the safety nets.
"""

from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

import ai_agent
import api
import inventory_ai as ai
from conftest import _close_current_db


# ─────────────── fake Claude ───────────────

def tool_use(*calls):
    return SimpleNamespace(stop_reason="tool_use", content=[
        SimpleNamespace(type="tool_use", id=f"toolu_{n}", name=name, input=args)
        for n, (name, args) in enumerate(calls)])


def says(text):
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


class FakeClaude:
    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.script.pop(0)

    def tool_results(self, request_index):
        """The tool_result blocks sent back to the model in a given request."""
        return self.requests[request_index]["messages"][-1]["content"]


# ─────────────── tools ───────────────

class TestTools:
    def test_inventory_lists_units_aliases_and_suppliers(self, downtown):
        inv = ai_agent.ToolBox("downtown").tool_get_inventory()
        carrot = next(i for i in inv["ingredients"] if i["name"] == "carrot")
        assert carrot["unit"] == "unit"
        assert "carrots" in carrot["aliases"]
        assert carrot["suppliers_in_region"] == 4
        assert carrot["cheapest"]["supplier"] == "BulkVeg"

    def test_add_new_ingredient_is_saved_and_understood(self, downtown, restart):
        box = ai_agent.ToolBox("downtown")
        out, err = box.run("add_ingredient", {"name": "Steak", "unit": "unit", "aliases": "steak,steaks"})
        assert not err
        assert ai.LOCATIONS["downtown"].inventory.get("steak").stock == 0
        assert ai._normalize_item("steaks") == "steak"
        assert restart()["downtown"].inventory.get("steak") is not None      # in the DB
        assert restart()["uptown"].inventory.get("steak") is None            # only this restaurant

    def test_add_existing_ingredient_fails(self, downtown):
        out, err = ai_agent.ToolBox("downtown").run("add_ingredient", {"name": "carrot", "unit": "unit", "aliases": ""})
        assert err and "already exists" in out

    def test_order_without_supplier_asks_instead_of_inventing(self, downtown):
        box = ai_agent.ToolBox("downtown")
        box.run("add_ingredient", {"name": "steak", "unit": "unit", "aliases": "steak"})
        out, err = box.run("place_order", {"ingredient": "steak", "quantity": 5})
        assert err and "No supplier" in out
        assert ai.LOCATIONS["downtown"].order_history == []

    def test_order_unknown_ingredient_fails(self, downtown):
        out, err = ai_agent.ToolBox("downtown").run("place_order", {"ingredient": "caviar", "quantity": 1})
        assert err and "add_ingredient" in out

    def test_order_goes_through_cheapest_supplier(self, downtown):
        out, err = ai_agent.ToolBox("downtown").run("place_order", {"ingredient": "carrots", "quantity": 100})
        assert not err and '"supplier": "BulkVeg"' in out
        assert ai.LOCATIONS["downtown"].order_history[0].trigger == "ai-chat"

    def test_ai_cannot_bypass_budget(self, uptown):
        uptown.spend_this_week = 795
        out, err = ai_agent.ToolBox("uptown").run("place_order", {"ingredient": "carrot", "quantity": 100})
        assert '"result": "blocked"' in out and "budget" in out
        assert uptown.order_history == []

    def test_ai_cannot_bypass_duplicate_cooldown_unless_forced(self, downtown):
        box = ai_agent.ToolBox("downtown")
        box.run("place_order", {"ingredient": "carrot", "quantity": 50})
        out, _ = box.run("place_order", {"ingredient": "carrot", "quantity": 50})
        assert '"result": "blocked"' in out
        out, _ = box.run("place_order", {"ingredient": "carrot", "quantity": 50, "force": True})
        assert '"result": "ordered"' in out

    def test_ai_cannot_bypass_capacity_cap(self, uptown):
        out, _ = ai_agent.ToolBox("uptown").run("order_for_people", {"ingredient": "chicken", "people": 9999})
        assert "SAFETY STOP" in out
        assert uptown.order_history == []

    def test_case_rounding_still_applies(self, downtown):
        box = ai_agent.ToolBox("downtown")
        box.run("add_ingredient", {"name": "crushed tomatoes can", "unit": "can", "aliases": "crushed tomatoes"})
        box.run("add_supplier", {"name": "Metro", "ingredient": "crushed_tomatoes_can", "price_per_unit": 1.5,
                                 "delivery_days": 1, "case_size": 6})
        out, err = box.run("place_order", {"ingredient": "crushed tomatoes", "quantity": 4})
        assert not err and '"quantity": 6' in out

    def test_sale_receive_cancel(self, downtown):
        box = ai_agent.ToolBox("downtown")
        out, _ = box.run("punch_sale", {"dish": "soup", "servings": 70})
        assert '"auto_orders"' in out and "carrot" in out
        first, second = ai.LOCATIONS["downtown"].order_history[:2]
        assert "marked received" in box.run("receive_order", {"order_id": first.order_id.lower()})[0]
        assert "cancelled" in box.run("cancel_order", {"order_id": second.order_id})[0]

    def test_cannot_touch_another_restaurants_order(self, downtown, uptown):
        order = uptown.place_order("carrot", 50, trigger="test")
        out, err = ai_agent.ToolBox("downtown").run("cancel_order", {"order_id": order.order_id})
        assert err and order.status == "pending"

    def test_bad_arguments_become_tool_errors(self, downtown):
        box = ai_agent.ToolBox("downtown")
        assert box.run("place_order", {"ingredient": "carrot"})[1]                    # missing quantity
        assert box.run("place_order", {"ingredient": "carrot", "quantity": -3})[1]    # negative
        assert box.run("teleport_food", {})[1]                                        # unknown tool


# ─────────────── agent loop ───────────────

class TestAgentLoop:
    def test_user_message_from_the_bug_report(self, downtown):
        """'give me 3 bag of carrot 5 steaks and 3 oil olive and i need some crush can'"""
        claude = FakeClaude(
            tool_use(("get_inventory", {})),
            tool_use(("add_ingredient", {"name": "steak", "unit": "unit", "aliases": "steak,steaks"}),
                     ("place_order", {"ingredient": "steak", "quantity": 5})),
            says("✅ steak added\n❓ Which supplier and price for the steaks? How many carrots per bag? "
                 "Olive oil: is it the existing 'oil'? What is 'crush can'?"),
        )
        result = ai_agent.chat("downtown", "give me 3 bag of carrot 5 steaks and 3 oil olive and i need some crush can",
                               client=claude)
        assert "Which supplier" in result["reply"]
        assert ai.LOCATIONS["downtown"].inventory.get("steak") is not None
        # both tool results went back in ONE message; the missing supplier came back as an error
        results = claude.tool_results(2)
        assert [r["tool_use_id"] for r in results] == ["toolu_0", "toolu_1"]
        assert results[1].get("is_error") and "No supplier" in results[1]["content"]
        # the request carried the tools, the system prompt and the refusal fallback
        req = claude.requests[0]
        assert req["model"] == ai_agent.MODEL and req["fallbacks"] == "default"
        assert "Downtown" in req["system"] and {t["name"] for t in req["tools"]} >= {"add_ingredient", "add_supplier"}

    def test_follow_up_answer_uses_history(self, downtown):
        ai_agent.ToolBox("downtown").run("add_ingredient", {"name": "steak", "unit": "unit", "aliases": "steaks"})
        history = [{"role": "user", "content": "5 steaks"},
                   {"role": "assistant", "content": "❓ Which supplier and price for the steaks?"}]
        claude = FakeClaude(
            tool_use(("add_supplier", {"name": "Metro", "ingredient": "steak", "price_per_unit": 8.5,
                                       "delivery_days": 1})),
            tool_use(("place_order", {"ingredient": "steak", "quantity": 5})),
            says("✅ 5 steaks from Metro, $42.50"),
        )
        ai_agent.chat("downtown", "Metro, 8.50 each, next day", history=history, client=claude)
        assert claude.requests[0]["messages"][:2] == history
        order = ai.LOCATIONS["downtown"].order_history[0]
        assert (order.ingredient, order.supplier.name, order.total_cost) == ("steak", "Metro", 42.5)

    def test_refusal_is_handled(self, downtown):
        claude = FakeClaude(SimpleNamespace(stop_reason="refusal", content=[]))
        assert "can't help" in ai_agent.chat("downtown", "...", client=claude)["reply"]

    def test_runaway_loop_is_stopped(self, downtown):
        claude = FakeClaude(*[tool_use(("get_inventory", {}))] * ai_agent.MAX_TOOL_ROUNDS)
        assert "too many steps" in ai_agent.chat("downtown", "hi", client=claude)["reply"]

    def test_history_is_cleaned(self):
        dirty = [{"role": "assistant", "content": "hi"}, {"role": "system", "content": "x"},
                 {"role": "user", "content": "  "}, {"role": "user", "content": "a"},
                 {"role": "assistant", "content": "b"}]
        assert ai_agent._clean_history(dirty) == [{"role": "user", "content": "a"},
                                                  {"role": "assistant", "content": "b"}]


# ─────────────── API ───────────────

@pytest.fixture
def client(db_url):
    with TestClient(api.app) as c:
        yield c
    _close_current_db()


def test_api_simple_mode_without_key(client, monkeypatch):
    monkeypatch.setattr(ai_agent, "is_configured", lambda: False)
    assert client.get("/ai-status").json()["mode"] == "simple"
    r = client.post("/chat", json={"message": "order 50 carrots"}).json()
    assert r["mode"] == "simple" and len(r["new_orders"]) == 1


def test_api_ai_mode_reports_new_orders(client, monkeypatch):
    monkeypatch.setattr(ai_agent, "is_configured", lambda: True)
    claude = FakeClaude(
        tool_use(("add_ingredient", {"name": "steak", "unit": "unit", "aliases": "steaks"})),
        tool_use(("add_supplier", {"name": "Metro", "ingredient": "steak", "price_per_unit": 8.5, "delivery_days": 1})),
        tool_use(("place_order", {"ingredient": "steak", "quantity": 5})),
        says("✅ 5 steaks from Metro"),
    )
    monkeypatch.setattr(ai_agent, "make_client", lambda: claude)
    r = client.post("/chat?location=uptown", json={"message": "5 steaks from Metro at 8.50", "history": []}).json()
    assert r["mode"] == "ai" and r["reply"] == "✅ 5 steaks from Metro"
    assert [(o["ingredient"], o["supplier"], o["location"]) for o in r["new_orders"]] == [("steak", "Metro", "uptown")]
    assert "steak" in [i["name"] for i in client.get("/stock?location=uptown").json()["items"]]


def test_api_falls_back_to_simple_mode_when_ai_unreachable(client, monkeypatch):
    monkeypatch.setattr(ai_agent, "is_configured", lambda: True)

    def offline(**kwargs):
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))

    monkeypatch.setattr(ai_agent, "make_client",
                        lambda: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=offline))))
    r = client.post("/chat", json={"message": "order 50 carrots"}).json()
    assert r["mode"] == "simple" and "AI unavailable" in r["reply"]
    assert len(r["new_orders"]) == 1                         # the kitchen keeps working
