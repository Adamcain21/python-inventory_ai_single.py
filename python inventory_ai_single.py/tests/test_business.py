"""
Business-logic tests for inventory_ai.py.
Demo data (see db.py): Downtown cap 150/day x3 = 450 people, budget $1500;
                       Uptown   cap  80/day x3 = 240 people, budget $800.
"""

from datetime import timedelta

import pytest

import db
import inventory_ai as ai


# ═══════════════════════════════════════════════════════════════
# 1. STOCK DEDUCTION ON SALE
# ═══════════════════════════════════════════════════════════════

class TestSaleDeduction:
    def test_sale_deducts_every_recipe_ingredient(self, downtown):
        downtown.punch_sale("carrot_soup", 10)   # carrot 1.0, oil 0.05, onion 0.1, bread 0.05 per serving
        inv = downtown.inventory
        assert inv.get("carrot").stock == pytest.approx(70)
        assert inv.get("oil").stock == pytest.approx(4.5)
        assert inv.get("onion").stock == pytest.approx(7)
        assert inv.get("bread").stock == pytest.approx(11.5)
        assert inv.get("chicken").stock == pytest.approx(10)   # not in the recipe
        assert downtown.order_history == []                    # nothing fell below its reorder point

    def test_dish_alias_works(self, downtown):
        downtown.punch_sale("soup", 1)
        assert downtown.inventory.get("carrot").stock == pytest.approx(79)

    def test_stock_never_goes_negative(self, downtown):
        downtown.punch_sale("roast_chicken", 50)               # only 10 chicken in stock
        assert downtown.inventory.get("chicken").stock == 0

    def test_low_stock_triggers_auto_order(self, downtown):
        downtown.punch_sale("carrot_soup", 70)                 # carrot 80 -> 10, below reorder point 20
        carrot_orders = [o for o in downtown.order_history if o.ingredient == "carrot"]
        assert len(carrot_orders) == 1
        order = carrot_orders[0]
        assert order.trigger == "auto-low-stock"
        assert order.quantity == 100                           # reorder_qty
        assert order.status == "pending"
        assert downtown.inventory.get("carrot").stock == pytest.approx(10)   # stock only moves on receipt

    def test_unknown_dish_changes_nothing(self, downtown):
        before = {n: i.stock for n, i in downtown.inventory.items.items()}
        downtown.punch_sale("pizza", 3)
        assert {n: i.stock for n, i in downtown.inventory.items.items()} == before

    def test_missing_ingredient_blocks_whole_sale(self, downtown, count_rows):
        del downtown.inventory.items["garlic"]
        downtown.punch_sale("garlic_bread", 2)                 # bread + garlic + oil
        assert downtown.inventory.get("bread").stock == pytest.approx(12)   # nothing half-deducted
        assert count_rows(db.SaleRow) == 0

    def test_sale_and_stock_are_saved_to_db(self, downtown, restart, count_rows):
        downtown.punch_sale("carrot_soup", 10)
        downtown = restart()["downtown"]
        assert downtown.inventory.get("carrot").stock == pytest.approx(70)
        assert downtown.inventory.get("oil").stock == pytest.approx(4.5)
        assert count_rows(db.SaleRow) == 1


# ═══════════════════════════════════════════════════════════════
# 2. CHEAPEST SUPPLIER (real total cost = price + delivery fee)
# ═══════════════════════════════════════════════════════════════

class TestSupplierChoice:
    def test_small_order_delivery_fee_decides(self, locations):
        # 10 carrots: LocalMarket 11.00 (no fee) < FarmCo 14.00 < FreshDirect 15.50 < BulkVeg 20.00
        assert ai.choose_supplier("carrot", 10).name == "LocalMarket"

    def test_big_order_cheapest_unit_price_wins(self, locations):
        # 100 carrots: BulkVeg 50.00 (free shipping >= 80) < FreshDirect 75 < FarmCo 95 < LocalMarket 110
        assert ai.choose_supplier("carrot", 100).name == "BulkVeg"

    def test_free_shipping_threshold_flips_the_winner(self, locations):
        # 14 chicken: BulkMeat pays its $20 fee -> 74.60, CityButcher 67.20 wins
        # 15 chicken: BulkMeat free shipping    -> 58.50, cheapest by far
        assert ai.choose_supplier("chicken", 14).name == "CityButcher"
        assert ai.choose_supplier("chicken", 15).name == "BulkMeat"

    def test_tie_within_margin_goes_to_faster_more_reliable(self, locations):
        ai.SUPPLIERS["saffron"] = [
            ai.Supplier("CheapSlow", 10.00, delivery_days=5, reliability_score=0.80),
            ai.Supplier("NearlySameFast", 10.50, delivery_days=1, reliability_score=0.99),   # +5% -> tie
            ai.Supplier("TooExpensive", 12.00, delivery_days=0.5, reliability_score=1.00),   # +20% -> out
        ]
        assert ai.choose_supplier("saffron", 1).name == "NearlySameFast"

    def test_place_order_uses_the_chosen_supplier(self, downtown):
        order = downtown.place_order("carrot", 10, trigger="test")
        assert order.supplier.name == "LocalMarket"
        assert order.total_cost == pytest.approx(11.00)

    def test_no_supplier_raises(self, downtown):
        with pytest.raises(ValueError):
            ai.choose_supplier("unobtainium", 1)


# ═══════════════════════════════════════════════════════════════
# 3. WEEKLY BUDGET
# ═══════════════════════════════════════════════════════════════

class TestWeeklyBudget:
    def test_order_over_budget_is_blocked_and_nothing_saved(self, uptown, count_rows):
        uptown.spend_this_week = 790                           # $10 left of $800
        with pytest.raises(ai.BudgetExceededError):
            uptown.place_order("chicken", 20, trigger="test")  # 20 x 3.90 BulkMeat = $78
        assert uptown.order_history == []
        assert uptown.spend_this_week == 790
        assert count_rows(db.OrderRow) == 0

    def test_order_exactly_at_budget_is_allowed(self, uptown):
        uptown.spend_this_week = 750
        uptown.place_order("carrot", 100, trigger="test")      # BulkVeg, $50.00
        assert uptown.spend_this_week == pytest.approx(800)

    def test_auto_order_skipped_when_budget_is_spent_but_sale_still_counts(self, uptown):
        uptown.spend_this_week = 800
        uptown.punch_sale("carrot_soup", 70)
        assert uptown.inventory.get("carrot").stock == pytest.approx(10)
        assert uptown.order_history == []

    def test_natural_language_order_over_budget_is_blocked(self, uptown):
        uptown.spend_this_week = 799
        reply = ai.handle_nl_request(uptown, "order 50 carrots")
        assert "ORDER BLOCKED" in reply
        assert uptown.order_history == []

    def test_spend_is_saved_to_db(self, downtown, restart):
        downtown.place_order("carrot", 100, trigger="test")
        assert restart()["downtown"].spend_this_week == pytest.approx(50)


# ═══════════════════════════════════════════════════════════════
# 4. CAPACITY CAP
# ═══════════════════════════════════════════════════════════════

class TestCapacity:
    def test_request_over_cap_is_stopped(self, uptown):
        reply = ai.handle_nl_request(uptown, "need chicken for 241 people")   # cap = 80 x 3 = 240
        assert "SAFETY STOP" in reply
        assert uptown.order_history == []

    def test_absurd_request_is_stopped(self, downtown):
        reply = ai.handle_nl_request(downtown, "need carrots for 10000 people")
        assert "SAFETY STOP" in reply
        assert downtown.order_history == []

    def test_request_at_cap_is_allowed(self, downtown):
        reply = ai.handle_nl_request(downtown, "need carrots for 450 people")  # cap = 150 x 3 = 450
        assert "SAFETY STOP" not in reply
        assert len(downtown.order_history) == 1
        assert downtown.order_history[0].quantity == pytest.approx(370)          # 450 needed - 80 in stock

    def test_check_capacity_raises(self, uptown):
        uptown.check_capacity(240)
        with pytest.raises(ai.CapacityExceededError):
            uptown.check_capacity(241)

    def test_multi_ingredient_request_each_checked(self, uptown):
        reply = ai.handle_nl_request(uptown, "need carrots and chicken for 500 people")
        assert reply.count("SAFETY STOP") == 2
        assert uptown.order_history == []


# ═══════════════════════════════════════════════════════════════
# 5. DUPLICATE-ORDER COOLDOWN
# ═══════════════════════════════════════════════════════════════

class TestDuplicateOrders:
    def test_second_order_same_ingredient_is_blocked(self, downtown):
        downtown.place_order("carrot", 50, trigger="test")
        with pytest.raises(ai.DuplicateOrderError):
            downtown.place_order("carrot", 50, trigger="test")
        assert len(downtown.order_history) == 1

    def test_other_ingredient_is_not_blocked(self, downtown):
        downtown.place_order("carrot", 50, trigger="test")
        downtown.place_order("onion", 10, trigger="test")
        assert len(downtown.order_history) == 2

    def test_force_bypasses_cooldown(self, downtown):
        downtown.place_order("carrot", 50, trigger="test")
        downtown.place_order("carrot", 50, trigger="test", force=True)
        assert len(downtown.order_history) == 2

    def test_allowed_again_after_cooldown(self, downtown):
        downtown.place_order("carrot", 50, trigger="test")
        downtown._last_order_time["carrot"] -= timedelta(hours=4, minutes=1)
        downtown.place_order("carrot", 50, trigger="test")
        assert len(downtown.order_history) == 2

    def test_cooldown_is_per_location(self, downtown, uptown):
        downtown.place_order("carrot", 50, trigger="test")
        uptown.place_order("carrot", 50, trigger="test")       # different restaurant -> fine
        assert len(uptown.order_history) == 1

    def test_cooldown_survives_restart(self, downtown, restart):
        downtown.place_order("carrot", 50, trigger="test")
        downtown = restart()["downtown"]
        with pytest.raises(ai.DuplicateOrderError):
            downtown.place_order("carrot", 50, trigger="test")

    def test_repeated_sales_order_only_once(self, downtown):
        downtown.punch_sale("carrot_soup", 70)                 # carrot drops below 20 -> auto-order
        downtown.punch_sale("carrot_soup", 5)                  # still low -> must NOT re-order
        assert len([o for o in downtown.order_history if o.ingredient == "carrot"]) == 1

    def test_natural_language_duplicate_is_blocked(self, downtown):
        ai.handle_nl_request(downtown, "order 50 carrots")
        reply = ai.handle_nl_request(downtown, "order 50 carrots")
        assert "ORDER BLOCKED" in reply
        assert len(downtown.order_history) == 1


# ═══════════════════════════════════════════════════════════════
# 6. CASE ROUNDING
# ═══════════════════════════════════════════════════════════════

def _only_oilpro():
    ai.SUPPLIERS["oil"] = [s for s in ai.SUPPLIERS["oil"] if s.name == "OilPro"]   # sells by cases of 12
    return ai.SUPPLIERS["oil"][0]


class TestCaseRounding:
    @pytest.mark.parametrize("needed, ordered", [(1, 12), (10, 12), (12, 12), (13, 24), (25, 36)])
    def test_round_up_to_whole_cases(self, locations, needed, ordered):
        assert _only_oilpro().round_to_case(needed) == ordered

    def test_supplier_without_case_size_sells_exact_amount(self, locations):
        local = next(s for s in ai.SUPPLIERS["oil"] if s.name == "LocalMarket")
        assert local.round_to_case(7.3) == 7.3

    def test_total_cost_is_for_whole_cases(self, locations):
        assert _only_oilpro().total_cost(10) == pytest.approx(12 * 4.10 + 5.0)

    def test_placed_order_is_rounded_and_saved(self, downtown, restart):
        _only_oilpro()
        order = downtown.place_order("oil", 10, trigger="test")
        assert order.quantity == 12
        assert order.total_cost == pytest.approx(54.20)
        assert downtown.spend_this_week == pytest.approx(54.20)
        saved = restart()["downtown"].order_history[0]
        assert saved.quantity == 12
        assert saved.supplier.name == "OilPro"


# ═══════════════════════════════════════════════════════════════
# 7. RECEIVE / CANCEL
# ═══════════════════════════════════════════════════════════════

class TestReceiveAndCancel:
    def test_receive_adds_stock(self, downtown):
        order = downtown.place_order("carrot", 50, trigger="test")
        assert downtown.inventory.get("carrot").stock == 80   # pending: no stock yet
        msg = downtown.receive_order(order.order_id)
        assert "marked received" in msg
        assert order.status == "received"
        assert downtown.inventory.get("carrot").stock == pytest.approx(130)
        assert downtown.inventory.get("carrot").last_ordered is not None

    def test_receive_twice_does_not_double_stock(self, downtown):
        order = downtown.place_order("carrot", 50, trigger="test")
        downtown.receive_order(order.order_id)
        msg = downtown.receive_order(order.order_id)
        assert "already received" in msg
        assert downtown.inventory.get("carrot").stock == pytest.approx(130)

    def test_receive_is_saved_to_db(self, downtown, restart):
        order = downtown.place_order("carrot", 50, trigger="test")
        downtown.receive_order(order.order_id)
        downtown = restart()["downtown"]
        assert downtown.inventory.get("carrot").stock == pytest.approx(130)
        assert downtown.order_history[0].status == "received"

    def test_cancel_refunds_budget_and_keeps_stock(self, downtown):
        order = downtown.place_order("carrot", 100, trigger="test")
        assert downtown.spend_this_week == pytest.approx(50)
        msg = downtown.cancel_order(order.order_id)
        assert "cancelled" in msg
        assert order.status == "cancelled"
        assert downtown.spend_this_week == pytest.approx(0)
        assert downtown.inventory.get("carrot").stock == 80

    def test_cancel_is_saved_to_db(self, downtown, restart):
        order = downtown.place_order("carrot", 100, trigger="test")
        downtown.cancel_order(order.order_id)
        downtown = restart()["downtown"]
        assert downtown.order_history[0].status == "cancelled"
        assert downtown.spend_this_week == pytest.approx(0)

    def test_cannot_cancel_received_order(self, downtown):
        order = downtown.place_order("carrot", 100, trigger="test")
        downtown.receive_order(order.order_id)
        msg = downtown.cancel_order(order.order_id)
        assert "can't cancel" in msg
        assert order.status == "received"
        assert downtown.spend_this_week == pytest.approx(50)   # not refunded

    def test_cannot_receive_cancelled_order(self, downtown):
        order = downtown.place_order("carrot", 100, trigger="test")
        downtown.cancel_order(order.order_id)
        msg = downtown.receive_order(order.order_id)
        assert "already cancelled" in msg
        assert downtown.inventory.get("carrot").stock == 80

    def test_cancel_twice_refunds_once(self, downtown):
        downtown.place_order("onion", 10, trigger="test")
        order = downtown.place_order("carrot", 100, trigger="test")
        downtown.cancel_order(order.order_id)
        downtown.cancel_order(order.order_id)
        assert downtown.spend_this_week == pytest.approx(downtown.order_history[0].total_cost)

    def test_unknown_order_id(self, downtown):
        assert "No order with id" in downtown.receive_order("ORD-99999")
        assert "No order with id" in downtown.cancel_order("ORD-99999")

    def test_order_ids_continue_after_restart(self, downtown, restart):
        assert downtown.place_order("carrot", 50, trigger="test").order_id == "ORD-00001"
        downtown = restart()["downtown"]
        assert downtown.place_order("onion", 10, trigger="test").order_id == "ORD-00002"
