"""
Inventory AI — REST API (FastAPI)
==================================
Thin HTTP layer over inventory_ai.py: no business logic lives here, every
endpoint calls the same Location / handle_nl_request code the CLI uses, so
every sale, order, receipt and cancellation is saved to SQLite immediately.

Run:   uvicorn api:app --reload
Web:   http://127.0.0.1:8000/       (static/index.html)
Docs:  http://127.0.0.1:8000/docs   (Swagger, generated automatically)

Every endpoint that works on a location takes an optional ?location=<key>
(e.g. ?location=uptown). Default: downtown.
"""

import base64
import binascii
import io
import os
import secrets
import threading
from contextlib import asynccontextmanager, contextmanager, redirect_stdout
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))   # ANTHROPIC_API_KEY, INVENTORY_ACCESS_CODE... (see .env.example)

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

import ai_agent
import db
import inventory_ai as ai
import price_import

WEB_UI = os.path.join(HERE, "static", "index.html")


# ═══════════════════════════════════════════════════════════════
# 1. APP + SHARED STATE
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    ai.load_from_db()    # INVENTORY_DB_URL env var picks the database, like the CLI
    if not os.environ.get("INVENTORY_ACCESS_CODE"):
        print("WARNING: no INVENTORY_ACCESS_CODE set - anyone on this network can use the app. "
              "Set one in the .env file before connecting tablets.")
    print(f"AI: {'Claude (' + ai_agent.MODEL + ')' if ai_agent.is_configured() else 'simple mode (no ANTHROPIC_API_KEY)'}")
    yield


app = FastAPI(
    title="Inventory AI",
    version="3.0",
    description="Kitchen inventory + automatic supplier ordering, multi-location. "
                "Same business logic as the CLI, stored in SQLite.",
    lifespan=lifespan,
)

# lets other software (POS, other screens) call the API from a browser on another address
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

OPEN_PATHS = {"/", "/favicon.ico", "/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}


@app.middleware("http")
async def require_access_code(request: Request, call_next):
    """If INVENTORY_ACCESS_CODE is set, every API call must send it in the X-Access-Code
    header (the web page asks for it once). Stops anyone else on the Wi-Fi from ordering.
    Not a replacement for real user accounts before going on the internet."""
    code = os.environ.get("INVENTORY_ACCESS_CODE", "")
    if code and request.method != "OPTIONS" and request.url.path not in OPEN_PATHS:
        given = request.headers.get("x-access-code", "")
        if not secrets.compare_digest(given.encode(), code.encode()):
            return JSONResponse({"detail": "Access code required."}, status_code=401)
    return await call_next(request)

_lock = threading.Lock()


@contextmanager
def _locked_capture():
    """Serialize access to the shared in-memory state + DB session, and
    capture what the business logic prints (order receipts, warnings)."""
    buf = io.StringIO()
    with _lock, redirect_stdout(buf):
        yield buf


def _log_lines(buf: io.StringIO) -> List[str]:
    return [line for line in buf.getvalue().splitlines() if line.strip()]


LocationParam = Query(None, description="Location key, e.g. 'downtown' or 'uptown'. Default: downtown.")


def _get_location(key: Optional[str]) -> Tuple[str, ai.Location]:
    if key is None:
        key = "downtown" if "downtown" in ai.LOCATIONS else next(iter(ai.LOCATIONS))
    key = key.lower().strip()
    loc = ai.LOCATIONS.get(key)
    if loc is None:
        raise HTTPException(404, f"Unknown location '{key}'. Available: {list(ai.LOCATIONS)}")
    return key, loc


def _find_order(order_id: str) -> Tuple[str, ai.Location, ai.Order]:
    order_id = order_id.strip().upper()
    for key, loc in ai.LOCATIONS.items():
        for o in loc.order_history:
            if o.order_id == order_id:
                return key, loc, o
    raise HTTPException(404, f"No order with id {order_id}.")


# ═══════════════════════════════════════════════════════════════
# 2. SCHEMAS (shown in Swagger)
# ═══════════════════════════════════════════════════════════════

class StockItemOut(BaseModel):
    name: str
    unit: str
    stock: float
    reorder_point: float
    reorder_qty: float
    low: bool


class StockOut(BaseModel):
    location: str
    name: str
    spend_this_week: float
    weekly_budget: float
    items: List[StockItemOut]


class LocationOut(BaseModel):
    key: str
    name: str
    region: Optional[str]
    max_capacity: int
    max_people_per_request: int
    weekly_budget: float
    spend_this_week: float


class DishOut(BaseModel):
    name: str
    ingredients: dict


class OrderOut(BaseModel):
    order_id: str
    location: str
    ingredient: str
    quantity: float
    unit: str
    supplier: str
    trigger: str
    unit_price: float
    total_cost: float
    placed_at: datetime
    expected_arrival: datetime
    status: str
    po_id: Optional[str] = Field(None, description="Purchase order (one delivery) this line belongs to")


class SaleIn(BaseModel):
    dish: str = Field(..., examples=["carrot_soup"], description="Dish name or alias (e.g. 'soup')")
    servings: int = Field(1, ge=1, examples=[3])


class DeductionOut(BaseModel):
    ingredient: str
    amount: float
    unit: str
    stock_after: float


class SaleOut(BaseModel):
    dish: str
    servings: int
    deducted: List[DeductionOut]
    new_orders: List[OrderOut]
    log: List[str]


class OrderIn(BaseModel):
    ingredient: str = Field(..., examples=["carrot"], description="Ingredient name or alias (e.g. 'carrots')")
    quantity: Optional[float] = Field(None, gt=0, description="Default: the ingredient's reorder quantity")
    force: bool = Field(False, description="Skip the duplicate-order cooldown (like 'force order' in the CLI)")
    prefer_speed: bool = Field(False, description="Widen the tie margin in favour of faster suppliers")


class ActionOut(BaseModel):
    message: str
    order: OrderOut
    log: List[str]


class ChatTurn(BaseModel):
    role: str = Field(..., examples=["user"], description="'user' or 'assistant'")
    content: str


class ChatIn(BaseModel):
    message: str = Field(..., examples=["give me 3 bags of carrots, 5 steaks and 3 olive oil"])
    history: List[ChatTurn] = Field([], description="Previous turns of this conversation (AI mode only), "
                                                   "so the AI can follow up on its own questions")


class ChatOut(BaseModel):
    reply: str
    mode: str = Field(..., description="'ai' (Claude) or 'simple' (English pattern matcher)")
    new_orders: List[OrderOut]
    log: List[str]


class AiStatusOut(BaseModel):
    mode: str
    model: Optional[str]


def _order_out(key: str, loc: ai.Location, o: ai.Order) -> OrderOut:
    item = loc.inventory.get(o.ingredient)
    return OrderOut(
        order_id=o.order_id, location=key, ingredient=o.ingredient, quantity=o.quantity,
        unit=item.unit if item else "", supplier=o.supplier.name, trigger=o.trigger,
        unit_price=o.unit_price, total_cost=o.total_cost, placed_at=o.placed_at,
        expected_arrival=o.expected_arrival, status=o.status, po_id=o.po_id,
    )


# ═══════════════════════════════════════════════════════════════
# 3. ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/locations", response_model=List[LocationOut], tags=["info"])
def get_locations():
    with _locked_capture():
        return [
            LocationOut(key=key, name=loc.name, region=loc.region, max_capacity=loc.max_capacity,
                        max_people_per_request=loc.max_people_per_request,
                        weekly_budget=loc.weekly_budget, spend_this_week=loc.spend_this_week)
            for key, loc in ai.LOCATIONS.items()
        ]


@app.get("/dishes", response_model=List[DishOut], tags=["info"])
def get_dishes():
    with _locked_capture():
        return [DishOut(name=name, ingredients=dict(ai.RECIPES[name])) for name in ai.list_dishes()]


@app.get("/stock", response_model=StockOut, tags=["stock"])
def get_stock(location: Optional[str] = LocationParam):
    """Current inventory of the location. `low` = stock below reorder point."""
    with _locked_capture():
        key, loc = _get_location(location)
        items = [
            StockItemOut(name=name, unit=i.unit, stock=round(i.stock, 4), reorder_point=i.reorder_point,
                         reorder_qty=i.reorder_qty, low=i.stock < i.reorder_point)
            for name, i in sorted(loc.inventory.items.items())
        ]
        return StockOut(location=key, name=loc.name, spend_this_week=round(loc.spend_this_week, 2),
                        weekly_budget=loc.weekly_budget, items=items)


@app.post("/sale", response_model=SaleOut, tags=["stock"])
def post_sale(body: SaleIn, location: Optional[str] = LocationParam):
    """Punch a sale: deducts the recipe's ingredients and places automatic
    orders for anything that falls below its reorder point."""
    with _locked_capture() as buf:
        key, loc = _get_location(location)
        dish = ai.resolve_dish_name(body.dish)
        if dish is None:
            raise HTTPException(404, f"Unknown dish: {body.dish}. Available: {ai.list_dishes()}")
        recipe = ai.get_recipe(dish)
        missing = [i for i in recipe if not loc.inventory.get(i)]
        if missing:
            raise HTTPException(400, f"{dish} uses {', '.join(missing)}, which is not in "
                                     f"{loc.name}'s inventory. Sale not recorded.")

        before = len(loc.order_history)
        loc.punch_sale(dish, body.servings)

        deducted = [
            DeductionOut(ingredient=ing, amount=round(per * body.servings, 4), unit=loc.inventory.get(ing).unit,
                         stock_after=round(loc.inventory.get(ing).stock, 4))
            for ing, per in recipe.items()
        ]
        return SaleOut(dish=dish, servings=body.servings, deducted=deducted,
                       new_orders=[_order_out(key, loc, o) for o in loc.order_history[before:]],
                       log=_log_lines(buf))


@app.post("/order", response_model=ActionOut, tags=["orders"])
def post_order(body: OrderIn, location: Optional[str] = LocationParam):
    """Place a manual order. The cheapest supplier (real total cost) is chosen
    automatically; budget and duplicate-order safety nets apply (409 if blocked)."""
    with _locked_capture() as buf:
        key, loc = _get_location(location)
        name = ai._normalize_item(body.ingredient) or body.ingredient.lower().strip()
        item = loc.inventory.get(name)
        if item is None:
            raise HTTPException(404, f"'{body.ingredient}' is not in {loc.name}'s inventory system.")
        try:
            order = loc.place_order(
                name, body.quantity or item.reorder_qty,
                trigger="human-request-forced" if body.force else "human-request",
                force=body.force, prefer_speed=body.prefer_speed,
            )
        except (ai.BudgetExceededError, ai.DuplicateOrderError, ai.MinimumOrderError) as e:
            raise HTTPException(409, f"ORDER BLOCKED: {e}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return ActionOut(message=f"Ordered {order.quantity:.2f} {item.unit} of {name} from {order.supplier.name}.",
                         order=_order_out(key, loc, order), log=_log_lines(buf))


@app.get("/orders", response_model=List[OrderOut], tags=["orders"])
def get_orders(location: Optional[str] = LocationParam,
               status: Optional[str] = Query(None, description="Filter: pending, received or cancelled")):
    """Order history of the location, oldest first."""
    with _locked_capture():
        key, loc = _get_location(location)
        return [_order_out(key, loc, o) for o in loc.order_history
                if status is None or o.status == status.lower()]


@app.post("/receive/{order_id}", response_model=ActionOut, tags=["orders"])
def post_receive(order_id: str):
    """Mark a pending order as received: its quantity is added to stock.
    409 if the order is already received or cancelled."""
    with _locked_capture() as buf:
        key, loc, order = _find_order(order_id)
        was_pending = order.status == "pending"
        message = loc.receive_order(order.order_id)
        if not was_pending:
            raise HTTPException(409, message)
        return ActionOut(message=message, order=_order_out(key, loc, order), log=_log_lines(buf))


@app.post("/cancel/{order_id}", response_model=ActionOut, tags=["orders"])
def post_cancel(order_id: str):
    """Cancel a pending order and refund it from the weekly spend.
    409 if the order is already received or cancelled."""
    with _locked_capture() as buf:
        key, loc, order = _find_order(order_id)
        was_pending = order.status == "pending"
        message = loc.cancel_order(order.order_id)
        if not was_pending:
            raise HTTPException(409, message)
        return ActionOut(message=message, order=_order_out(key, loc, order), log=_log_lines(buf))


@app.get("/ai-status", response_model=AiStatusOut, tags=["chat"])
def get_ai_status():
    """'ai' when an Anthropic API key is configured, otherwise 'simple'."""
    return AiStatusOut(mode="ai", model=ai_agent.MODEL) if ai_agent.is_configured() \
        else AiStatusOut(mode="simple", model=None)


def _ai_error_reason(e: Exception) -> str:
    if "credit balance" in str(e).lower():
        return "no credit left on the Anthropic account (console.anthropic.com -> Billing)"
    if type(e).__name__ == "APIConnectionError":
        return "no internet connection"
    return type(e).__name__


def _simple_chat(key: str, message: str, note: str = "") -> Tuple[str, List[str]]:
    with _locked_capture() as buf:
        reply = ai.handle_nl_request(ai.LOCATIONS[key], message)
    return (note + reply if note else reply), _log_lines(buf)


@app.post("/chat", response_model=ChatOut, tags=["chat"])
def post_chat(body: ChatIn, location: Optional[str] = LocationParam):
    """Talk to the AI in natural language.

    - **AI mode** (ANTHROPIC_API_KEY set): Claude understands free-form requests in any language,
      can add new ingredients/suppliers, and asks questions when something is unclear.
      Send the previous turns in `history` so it can follow up.
    - **Simple mode**: same as `text <message>` in the CLI ('need green beans for 30 people',
      'order 50 carrots', 'we are low on bread')."""
    with _locked_capture():
        key, _ = _get_location(location)
        order_ids_before = {o.order_id for o in ai.LOCATIONS[key].order_history}

    mode = "simple"
    if not ai_agent.is_configured():
        reply, log = _simple_chat(key, body.message)
        if "don't recognize" in reply or "could not understand" in reply or "not in" in reply:
            reply += ("\n\n(Simple mode only understands fixed English phrases and existing ingredients. "
                      "Set ANTHROPIC_API_KEY and restart the server to enable the real AI.)")
    else:
        import anthropic
        try:
            result = ai_agent.chat(key, body.message, [t.model_dump() for t in body.history],
                                   guard=_locked_capture)
            reply, log, mode = result["reply"], result["log"], "ai"
        except anthropic.AuthenticationError:
            raise HTTPException(502, "The Anthropic API key is invalid. Check ANTHROPIC_API_KEY.")
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
            # AI unreachable (no internet, no credit, overloaded...): keep the kitchen working in simple mode
            reply, log = _simple_chat(key, body.message, note=f"(AI unavailable: {_ai_error_reason(e)} - "
                                                                f"simple mode, English only)\n")

    with _locked_capture():
        loc = ai.LOCATIONS[key]   # re-read: the AI may have reloaded the data (new ingredient/supplier)
        new_orders = [_order_out(key, loc, o) for o in loc.order_history if o.order_id not in order_ids_before]
    return ChatOut(reply=reply, mode=mode, new_orders=new_orders, log=log)


# ═══════════════════════════════════════════════════════════════
# 4. CONFIGURATION  (ingredients, suppliers, recipes)
#    Each edit is written to the DB, then the app reloads from it.
# ═══════════════════════════════════════════════════════════════

class IngredientConfig(BaseModel):
    name: str = Field(..., examples=["tomato"])
    unit: str = Field(..., examples=["kg"])
    stock: float = Field(..., ge=0)
    reorder_point: float = Field(..., ge=0)
    reorder_qty: float = Field(..., gt=0)
    min_order_qty: float = Field(1.0, gt=0)
    shelf_life_days: Optional[float] = Field(None, gt=0, description="Empty = doesn't spoil")
    daily_usage_estimate: float = Field(0.0, ge=0)
    aliases: str = Field("", description="Comma-separated words the AI understands, e.g. 'tomato,tomatoes'")


class SupplierConfig(BaseModel):
    name: str = Field(..., examples=["FarmCo"])
    ingredient_name: str = Field(..., examples=["tomato"])
    price_per_unit: float = Field(..., gt=0)
    delivery_days: float = Field(..., ge=0)
    reliability_score: float = Field(..., ge=0, le=1)
    delivery_fee: float = Field(0.0, ge=0)
    free_shipping_at: Optional[float] = Field(None, gt=0)
    min_order: float = Field(1.0, gt=0)
    case_size: Optional[float] = Field(None, gt=0)
    notes: str = ""
    region_code: Optional[str] = Field(None, examples=["QC"], description="Region served. Empty = all regions")


class SupplierConfigOut(SupplierConfig):
    id: int
    price_source: str = Field(..., description="'estimate' (catalog guess) or 'confirmed' (entered by the restaurant)")
    price_updated_at: Optional[datetime] = Field(None, description="Date of the invoice / entry of a confirmed price")


class RecipeConfig(BaseModel):
    name: str = Field(..., examples=["tomato_salad"])
    aliases: str = Field("", description="Comma-separated, e.g. 'tomato salad,salad'")
    ingredients: Dict[str, float] = Field(..., examples=[{"tomato": 0.2, "oil": 0.02}],
                                          description="ingredient -> quantity per serving")


def _config_write(fn, *args):
    """Run a db.* config function, then reload everything into memory."""
    try:
        result = fn(ai.DB_SESSION, *args)
    except db.ConfigError as e:
        raise HTTPException(400, str(e))
    ai.reload_from_db()
    return result


def _ingredient_config(row: db.IngredientRow) -> IngredientConfig:
    return IngredientConfig(name=row.name, unit=row.unit, stock=row.stock, reorder_point=row.reorder_point,
                            reorder_qty=row.reorder_qty, min_order_qty=row.min_order_qty,
                            shelf_life_days=row.shelf_life_days, daily_usage_estimate=row.daily_usage_estimate,
                            aliases=row.aliases)


def _supplier_config(row: db.SupplierRow) -> SupplierConfigOut:
    return SupplierConfigOut(id=row.id, name=row.name, ingredient_name=row.ingredient_name,
                             price_per_unit=row.price_per_unit, delivery_days=row.delivery_days,
                             reliability_score=row.reliability_score, delivery_fee=row.delivery_fee,
                             free_shipping_at=row.free_shipping_at, min_order=row.min_order,
                             case_size=row.case_size, notes=row.notes, region_code=row.region_code,
                             price_source=row.price_source, price_updated_at=row.price_updated_at)


def _recipe_config(row: db.RecipeRow) -> RecipeConfig:
    return RecipeConfig(name=row.name, aliases=row.aliases,
                        ingredients={i.ingredient_name: i.qty_per_serving for i in row.items})


@app.get("/config/ingredients", response_model=List[IngredientConfig], tags=["config"])
def get_config_ingredients(location: Optional[str] = LocationParam):
    with _locked_capture():
        _, loc = _get_location(location)
        rows = ai.DB_SESSION.scalars(select(db.IngredientRow)
                                     .where(db.IngredientRow.location_id == loc.db_id)
                                     .order_by(db.IngredientRow.name))
        return [_ingredient_config(r) for r in rows]


@app.put("/config/ingredients", response_model=IngredientConfig, tags=["config"])
def put_config_ingredient(body: IngredientConfig, location: Optional[str] = LocationParam):
    """Add or update an ingredient at this location (matched by name). Also used to correct stock by hand."""
    with _locked_capture():
        _, loc = _get_location(location)
        return _ingredient_config(_config_write(db.upsert_ingredient, loc.db_id, body.model_dump()))


@app.delete("/config/ingredients/{name}", tags=["config"])
def delete_config_ingredient(name: str, location: Optional[str] = LocationParam):
    with _locked_capture():
        _, loc = _get_location(location)
        _config_write(db.delete_ingredient, loc.db_id, name)
        return {"message": f"Ingredient '{name}' deleted from {loc.name}."}


@app.get("/config/suppliers", response_model=List[SupplierConfigOut], tags=["config"])
def get_config_suppliers(ingredient: Optional[str] = Query(None, description="Only offers for this ingredient"),
                         region: Optional[str] = Query(None, description="Only offers serving this region "
                                                                         "(plus those serving every region)")):
    with _locked_capture():
        query = select(db.SupplierRow).order_by(db.SupplierRow.ingredient_name, db.SupplierRow.price_per_unit)
        if ingredient:
            query = query.where(db.SupplierRow.ingredient_name == db.norm_name(ingredient))
        if region:
            query = query.where((db.SupplierRow.region_code == region.upper()) | db.SupplierRow.region_code.is_(None))
        return [_supplier_config(r) for r in ai.DB_SESSION.scalars(query)]


@app.post("/config/suppliers", response_model=SupplierConfigOut, tags=["config"])
def post_config_supplier(body: SupplierConfig):
    with _locked_capture():
        return _supplier_config(_config_write(db.upsert_supplier, None, body.model_dump()))


@app.put("/config/suppliers/{supplier_id}", response_model=SupplierConfigOut, tags=["config"])
def put_config_supplier(supplier_id: int, body: SupplierConfig):
    with _locked_capture():
        return _supplier_config(_config_write(db.upsert_supplier, supplier_id, body.model_dump()))


@app.delete("/config/suppliers/{supplier_id}", tags=["config"])
def delete_config_supplier(supplier_id: int):
    with _locked_capture():
        _config_write(db.delete_supplier, supplier_id)
        return {"message": f"Supplier {supplier_id} deleted."}


@app.get("/config/recipes", response_model=List[RecipeConfig], tags=["config"])
def get_config_recipes():
    with _locked_capture():
        return [_recipe_config(r) for r in ai.DB_SESSION.scalars(select(db.RecipeRow).order_by(db.RecipeRow.id))]


@app.put("/config/recipes", response_model=RecipeConfig, tags=["config"])
def put_config_recipe(body: RecipeConfig):
    """Add or update a recipe (matched by name). The ingredient list is replaced entirely."""
    if any(q <= 0 for q in body.ingredients.values()):
        raise HTTPException(422, "Every ingredient quantity must be greater than 0.")
    with _locked_capture():
        return _recipe_config(_config_write(db.upsert_recipe, body.name, body.aliases, body.ingredients))


@app.delete("/config/recipes/{name}", tags=["config"])
def delete_config_recipe(name: str):
    with _locked_capture():
        _config_write(db.delete_recipe, name)
        return {"message": f"Recipe '{name}' deleted."}


# ═══════════════════════════════════════════════════════════════
# 5. REGIONS, PRICE COMPARISON, NEW RESTAURANTS
# ═══════════════════════════════════════════════════════════════

class RegionOut(BaseModel):
    code: str
    name: str
    currency: str
    suppliers: int = Field(..., description="Number of distinct suppliers serving this region")
    restaurants: List[str]


class PriceOffer(BaseModel):
    supplier: str
    price_per_unit: float
    delivery_days: float
    delivery_fee: float
    case_size: Optional[float]
    total_cost: float = Field(..., description="Real total for the requested quantity (case rounding + fee)")
    estimated: bool
    price_age_days: Optional[int] = Field(None, description="Age of a confirmed price, in days")
    notes: str


class CatalogProduct(BaseModel):
    product: str
    offers: int
    cheapest_supplier: str
    min_price: float
    avg_price: float
    max_price: float
    estimated: bool = Field(..., description="True if the cheapest price is an estimate")


class NewLocationIn(BaseModel):
    key: str = Field(..., examples=["toronto"])
    name: str = Field(..., examples=["Toronto King St"])
    region_code: str = Field(..., examples=["ON"])
    max_capacity: int = Field(..., gt=0)
    weekly_budget: float = Field(..., gt=0)
    copy_ingredients_from: Optional[str] = Field(None, description="Key of an existing restaurant whose "
                                                                   "ingredient list is copied (stock 0)")


@app.get("/regions", response_model=List[RegionOut], tags=["regions"])
def get_regions():
    with _locked_capture():
        out = []
        for r in ai.DB_SESSION.scalars(select(db.RegionRow).order_by(db.RegionRow.code)):
            names = set(ai.DB_SESSION.scalars(select(db.SupplierRow.name).where(db.SupplierRow.region_code == r.code)))
            out.append(RegionOut(code=r.code, name=r.name, currency=r.currency, suppliers=len(names),
                                 restaurants=[k for k, loc in ai.LOCATIONS.items() if loc.region == r.code]))
        return out


@app.get("/labels", tags=["regions"])
def get_labels():
    """French name and search words of every catalog product: {"honey": {"fr": "Miel", "words": [...]}}."""
    from regional_catalog import FRENCH_NAMES, PRODUCTS, search_words
    return {p: {"fr": FRENCH_NAMES.get(p, p.replace("_", " ")), "words": search_words(p)} for p in PRODUCTS}


@app.get("/catalog", response_model=List[CatalogProduct], tags=["regions"])
def get_catalog(region: str = Query(..., examples=["QC"])):
    """Price overview of every product sold in a region: cheapest supplier and price range."""
    with _locked_capture():
        out = []
        for product in sorted(ai.SUPPLIERS):
            offers = [s for s in ai.suppliers_for(product, region.upper())]
            if not offers:
                continue
            prices = [s.price_per_unit for s in offers]
            best = min(offers, key=lambda s: s.price_per_unit)
            out.append(CatalogProduct(product=product, offers=len(offers), cheapest_supplier=best.name,
                                      min_price=min(prices), avg_price=round(sum(prices) / len(prices), 3),
                                      max_price=max(prices), estimated=best.price_source == "estimate"))
        return out


@app.get("/catalog/{product}", response_model=List[PriceOffer], tags=["regions"])
def get_catalog_product(product: str, region: str = Query(..., examples=["QC"]),
                        quantity: float = Query(1, gt=0, description="Quantity to price (in the product's unit)")):
    """Every supplier's offer for one product in a region, cheapest real total first."""
    with _locked_capture():
        name = ai._normalize_item(product) or db.norm_name(product)
        offers = ai.suppliers_for(name, region.upper())
        if not offers:
            raise HTTPException(404, f"No supplier sells '{product}' in region {region.upper()}.")
        rows = [PriceOffer(supplier=s.name, price_per_unit=s.price_per_unit, delivery_days=s.delivery_days,
                           delivery_fee=s.delivery_fee, case_size=s.case_size,
                           total_cost=round(s.total_cost(quantity), 2), estimated=s.price_source == "estimate",
                           price_age_days=price_import.price_age_days(s), notes=s.notes) for s in offers]
        return sorted(rows, key=lambda r: r.total_cost)


@app.put("/config/locations", response_model=LocationOut, tags=["config"])
def put_config_location(body: NewLocationIn):
    """Open a new restaurant in a region (e.g. your first one in Ontario)."""
    with _locked_capture():
        copy_from = None
        if body.copy_ingredients_from:
            _, source = _get_location(body.copy_ingredients_from)
            copy_from = source.db_id
        data = body.model_dump(exclude={"copy_ingredients_from"})
        data["region_code"] = data["region_code"].upper()
        row = _config_write(db.create_location, data, copy_from)
        loc = ai.LOCATIONS[row.key]
        return LocationOut(key=row.key, name=loc.name, region=loc.region, max_capacity=loc.max_capacity,
                           max_people_per_request=loc.max_people_per_request,
                           weekly_budget=loc.weekly_budget, spend_this_week=loc.spend_this_week)


# ═══════════════════════════════════════════════════════════════
# 5b. SMART ORDERING: baskets grouped per supplier, delivery minimums, suggestions
# ═══════════════════════════════════════════════════════════════

class BasketItem(BaseModel):
    ingredient: str = Field(..., examples=["carrot"])
    quantity: Optional[float] = Field(None, gt=0, description="Default: the ingredient's reorder quantity")


class BasketIn(BaseModel):
    items: List[BasketItem]
    force: bool = Field(False, description="Skip the duplicate-order cooldown")
    allow_below_minimum: bool = Field(False, description="Order even below a supplier's delivery minimum")


class TermsIn(BaseModel):
    supplier: str = Field(..., examples=["Sysco Québec"])
    min_order_value: float = Field(0, ge=0, description="$ of goods needed for a delivery")
    delivery_fee: float = Field(0, ge=0, description="$ per delivery")
    free_delivery_over: Optional[float] = Field(None, ge=0, description="$ of goods from which delivery is free")


def _basket_items(loc: ai.Location, items: List[BasketItem]) -> Dict[str, float]:
    wanted: Dict[str, float] = {}
    for it in items:
        name = ai._normalize_item(it.ingredient) or db.norm_name(it.ingredient)
        item = loc.inventory.get(name)
        if item is None:
            raise HTTPException(404, f"'{it.ingredient}' is not in {loc.name}'s inventory system.")
        wanted[name] = wanted.get(name, 0.0) + (it.quantity or item.reorder_qty)
    return wanted


def _basket_out(key: str, loc: ai.Location, result: ai.BasketResult, log) -> dict:
    return {"orders": [_order_out(key, loc, o).model_dump() for o in result.orders],
            "purchase_orders": [{"po_id": p.po_id, "supplier": p.supplier_name, "delivery_fee": p.delivery_fee}
                                for p in result.purchase_orders],
            "skipped": [{"ingredient": i, "reason": r} for i, r in result.skipped],
            "total_cost": result.total_cost, "log": log}


@app.post("/basket/preview", tags=["orders"])
def post_basket_preview(body: BasketIn, location: Optional[str] = LocationParam):
    """Plan several items WITHOUT ordering: one delivery per supplier, delivery fee counted once,
    minimum order of each supplier checked."""
    with _locked_capture():
        _, loc = _get_location(location)
        return ai.plan_purchase(_basket_items(loc, body.items), loc.region,
                                open_deliveries=loc.open_delivery_values()).to_dict()


@app.post("/basket", tags=["orders"])
def post_basket(body: BasketIn, location: Optional[str] = LocationParam):
    """Order several items at once, grouped per supplier. Orders what meets the delivery
    minimums and the budget; the rest comes back in `skipped` with the reason."""
    with _locked_capture() as buf:
        key, loc = _get_location(location)
        result = loc.place_basket(_basket_items(loc, body.items), trigger="human-request", force=body.force,
                                  allow_below_minimum=body.allow_below_minimum, partial=True)
        return _basket_out(key, loc, result, _log_lines(buf))


@app.get("/suggested-order", tags=["orders"])
def get_suggested_order(location: Optional[str] = LocationParam):
    """What should be ordered now: items below their reorder point with nothing on the way,
    planned per supplier, plus items getting low that could top up a delivery to its minimum."""
    with _locked_capture():
        _, loc = _get_location(location)
        needed, could_add = loc.suggested_order()
        plan = ai.plan_purchase(needed, loc.region, open_deliveries=loc.open_delivery_values()) \
            if needed else ai.PurchasePlan([], [])
        extras = {}
        for g in plan.groups:
            if g.below_minimum:
                extras[g.supplier_name] = [n for n in could_add
                                           if any(s.name == g.supplier_name for s in ai.suppliers_for(n, loc.region))]
        return {"needed": needed, "plan": plan.to_dict(), "could_add": could_add, "could_add_by_supplier": extras}


@app.post("/suggested-order/place", tags=["orders"])
def post_suggested_order(location: Optional[str] = LocationParam):
    """Order the suggestion: every delivery that meets its minimum and fits the budget."""
    with _locked_capture() as buf:
        key, loc = _get_location(location)
        needed, _ = loc.suggested_order()
        if not needed:
            return _basket_out(key, loc, ai.BasketResult(), [])
        result = loc.place_basket(needed, trigger="suggested-order", partial=True)
        return _basket_out(key, loc, result, _log_lines(buf))


@app.post("/receive-po/{po_id}", tags=["orders"])
def post_receive_po(po_id: str):
    """Receive every pending line of a purchase order (one delivery) at once."""
    with _locked_capture() as buf:
        po_id = po_id.strip().upper()
        for key, loc in ai.LOCATIONS.items():
            if po_id in loc.purchase_orders:
                return {"message": loc.receive_purchase_order(po_id), "log": _log_lines(buf)}
        raise HTTPException(404, f"No purchase order {po_id}.")


@app.get("/config/supplier-terms", tags=["config"])
def get_supplier_terms(region: Optional[str] = Query(None, examples=["QC"])):
    """Delivery conditions per supplier: minimum order, fee per delivery, free delivery threshold."""
    with _locked_capture():
        rows = [t for t in ai.SUPPLIER_TERMS.values() if region is None or t.region in (region.upper(), None)]
        return [{"supplier": t.supplier, "region": t.region, "min_order_value": t.min_order_value,
                 "delivery_fee": t.delivery_fee, "free_delivery_over": t.free_delivery_over,
                 "estimated": t.price_source == "estimate"} for t in sorted(rows, key=lambda t: t.supplier)]


@app.put("/config/supplier-terms", tags=["config"])
def put_supplier_terms(body: TermsIn, location: Optional[str] = LocationParam):
    """Set a supplier's real delivery conditions for this restaurant's region (becomes 'confirmed')."""
    with _locked_capture():
        _, loc = _get_location(location)
        row = _config_write(db.upsert_supplier_terms, body.supplier, loc.region, body.min_order_value,
                            body.delivery_fee, body.free_delivery_over)
        return {"supplier": row.name, "region": row.region_code, "min_order_value": row.min_order_value,
                "delivery_fee": row.delivery_fee, "free_delivery_over": row.free_delivery_over, "estimated": False}


# ═══════════════════════════════════════════════════════════════
# 6. REAL PRICES FROM INVOICES / PRICE LISTS  (see price_import.py)
# ═══════════════════════════════════════════════════════════════

class PriceFileIn(BaseModel):
    filename: str = Field(..., examples=["sysco-facture-0923.jpg"])
    media_type: str = Field(..., examples=["image/jpeg"], description="image/jpeg, image/png, image/webp, "
                                                                     "application/pdf or text/csv")
    data_base64: str


class PriceLineIn(BaseModel):
    ingredient: str
    unit: str = ""
    price_per_unit: float = Field(..., gt=0)
    case_size: Optional[float] = Field(None, gt=0)
    add_new: bool = Field(False, description="Create the ingredient if it isn't in the inventory")


class PriceApplyIn(BaseModel):
    supplier_name: str
    document_date: Optional[str] = Field(None, examples=["2026-09-23"])
    delivery_days: float = Field(1.0, ge=0, description="Used only for a supplier new to this region")
    lines: List[PriceLineIn]


@app.post("/prices/import", tags=["prices"])
def post_prices_import(body: PriceFileIn, location: Optional[str] = LocationParam):
    """Step 1: the AI reads an invoice / price list (photo, PDF or CSV) and returns a PREVIEW.
    Nothing is saved. Needs ANTHROPIC_API_KEY."""
    if not ai_agent.is_configured():
        raise HTTPException(503, "Reading invoices needs the AI: set ANTHROPIC_API_KEY in the .env file.")
    try:
        data = base64.b64decode(body.data_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "The file is not valid base64.")
    with _locked_capture():
        key, _ = _get_location(location)
    import anthropic
    try:
        return price_import.extract_prices(key, body.media_type.lower(), data, guard=_locked_capture)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except anthropic.AuthenticationError:
        raise HTTPException(502, "The Anthropic API key is invalid. Check ANTHROPIC_API_KEY.")
    except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
        raise HTTPException(502, f"AI unavailable: {_ai_error_reason(e)}.")


@app.post("/prices/apply", tags=["prices"])
def post_prices_apply(body: PriceApplyIn, location: Optional[str] = LocationParam):
    """Step 2: save the checked lines as CONFIRMED prices dated from the document."""
    with _locked_capture():
        key, _ = _get_location(location)
        try:
            return price_import.apply_prices(key, body.supplier_name, body.document_date,
                                             [line.model_dump() for line in body.lines], body.delivery_days)
        except db.ConfigError as e:
            raise HTTPException(400, str(e))


# ═══════════════════════════════════════════════════════════════
# 7. WEB UI  (static/index.html, served at http://127.0.0.1:8000/)
# ═══════════════════════════════════════════════════════════════

@app.get("/", include_in_schema=False)
def web_ui():
    return FileResponse(WEB_UI)
