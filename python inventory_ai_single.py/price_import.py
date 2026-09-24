"""
Inventory AI — real supplier prices from invoices and price lists
=================================================================
Distributors don't publish their prices, but every restaurant already receives
them: on each invoice, and in the weekly price lists Sysco / GFS / Colabor send.

  1. extract_prices()  Claude reads a photo (JPG/PNG/WEBP), a PDF or a CSV and
                       returns a PREVIEW: supplier, date, one line per product with
                       the price converted to the ingredient's unit. Nothing saved.
  2. The restaurant checks / corrects the preview in the web UI.
  3. apply_prices()    Saves each line as a CONFIRMED, dated price for that supplier
                       (updates the existing offer, or creates it).
"""

import base64
import json
from datetime import datetime
from typing import List, Optional

import ai_agent
import db
import inventory_ai as ai
from regional_catalog import PRODUCTS

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_BYTES = 20 * 1024 * 1024
STALE_AFTER_DAYS = 30

EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["supplier_name", "supplier_match", "document_date", "document_type", "lines"],
    "properties": {
        "supplier_name": {"type": "string", "description": "Supplier name as printed on the document"},
        "supplier_match": {"type": ["string", "null"],
                           "description": "Exact name from the known supplier list if it is the same company, else null"},
        "document_date": {"type": ["string", "null"], "description": "Invoice / price list date, YYYY-MM-DD"},
        "document_type": {"type": "string", "enum": ["invoice", "price_list", "other"]},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["description", "ingredient", "is_new", "unit", "price_per_unit", "case_size",
                             "confidence", "note"],
                "properties": {
                    "description": {"type": "string", "description": "Product line as printed"},
                    "ingredient": {"type": "string", "description": "Matching inventory/catalog name, or a new "
                                                                    "short lowercase_underscore name"},
                    "is_new": {"type": "boolean", "description": "True if not in the inventory list"},
                    "unit": {"type": "string", "description": "Unit the price is expressed in"},
                    "price_per_unit": {"type": "number", "description": "Price for ONE unit (before taxes)"},
                    "case_size": {"type": ["number", "null"], "description": "Units per case/pack, if sold by case"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "note": {"type": "string", "description": "How the price was converted, or what is unclear"},
                },
            },
        },
    },
}

PROMPT = """\
This is a supplier invoice or price list for the restaurant "{location}" (region {region}). \
Extract every FOOD product line with its price, so the restaurant's supplier prices stay up to date.

Match each line to the restaurant's ingredients when it is the same product, and express the \
price in THAT ingredient's unit. Convert packs: e.g. "canola oil 4x4L case $38.00" for an \
ingredient counted in L -> price_per_unit 2.375, case_size 16, note "38.00 / 16 L". If a \
product is not in the inventory, use the catalog name and unit when it matches, otherwise a \
new short lowercase_underscore name and the unit printed on the document; set is_new=true.

Skip taxes, deposits, delivery fees, credits and non-food items. Use prices before taxes. If \
a line is hard to read or the conversion is uncertain, still include it with confidence \
"low" and explain in note.

Restaurant ingredients (name: unit):
{inventory}

Regional catalog products (name: unit):
{catalog}

Known suppliers in this region:
{suppliers}"""


def _document_block(media_type: str, data: bytes) -> dict:
    encoded = base64.standard_b64encode(data).decode()
    if media_type in IMAGE_TYPES:
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}}
    if media_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": encoded}}
    if media_type.startswith("text/"):   # CSV / plain-text exports of a price list
        return {"type": "text", "text": "Document content:\n" + data.decode("utf-8", errors="replace")}
    raise ValueError(f"Unsupported file type '{media_type}'. Use a photo (JPG/PNG/WEBP), a PDF or a CSV.")


def extract_prices(location_key: str, media_type: str, data: bytes, client=None,
                   guard=ai_agent._capture) -> dict:
    """Read the document with Claude and return a preview. Nothing is saved.
    `guard` wraps the parts that read shared state (the API passes its lock);
    the slow AI call itself runs outside it."""
    if len(data) > MAX_BYTES:
        raise ValueError("File too large (max 20 MB).")
    document = _document_block(media_type, data)
    with guard():
        loc = ai.LOCATIONS[location_key]
        known_suppliers = sorted({s.name for offers in ai.SUPPLIERS.values() for s in offers
                                  if s.region in (loc.region, None)})
        prompt = PROMPT.format(
            location=loc.name, region=loc.region,
            inventory="\n".join(f"- {n}: {i.unit}" for n, i in sorted(loc.inventory.items.items())) or "(empty)",
            catalog="\n".join(f"- {n}: {spec[0]}" for n, spec in sorted(PRODUCTS.items())),
            suppliers="\n".join(f"- {s}" for s in known_suppliers) or "(none)",
        )
    client = client or ai_agent.make_client()
    response = client.beta.messages.create(
        model=ai_agent.MODEL,
        max_tokens=16000,
        betas=[ai_agent.FALLBACK_BETA],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
        messages=[{"role": "user", "content": [document, {"type": "text", "text": prompt}]}],
    )
    if response.stop_reason == "refusal":
        raise ValueError("The AI could not read this document.")
    if response.stop_reason == "max_tokens":
        raise ValueError("Document too long: split it into several files.")
    extracted = json.loads(next(b.text for b in response.content if b.type == "text"))

    supplier = extracted["supplier_match"] if extracted["supplier_match"] in known_suppliers else None
    lines = []
    with guard():
        loc = ai.LOCATIONS[location_key]
        for line in extracted["lines"]:
            name = db.norm_name(line["ingredient"])
            item = loc.inventory.get(name)
            current = db.find_supplier_offer(ai.DB_SESSION, supplier or extracted["supplier_name"], name, loc.region)
            lines.append({
                **line,
                "ingredient": name,
                "is_new": item is None,
                "inventory_unit": item.unit if item else PRODUCTS.get(name, (None,))[0],
                "current_price": current.price_per_unit if current else None,
                "current_price_source": current.price_source if current else None,
            })
    return {"supplier_name": supplier or extracted["supplier_name"], "known_supplier": supplier is not None,
            "document_date": extracted["document_date"], "document_type": extracted["document_type"],
            "lines": lines}


def _parse_date(text: Optional[str]) -> datetime:
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d") if text else datetime.now()
    except ValueError:
        parsed = datetime.now()
    return min(parsed, datetime.now())   # a typo'd future date must not look "fresh" forever


def apply_prices(location_key: str, supplier_name: str, document_date: Optional[str], lines: List[dict],
                 delivery_days: float = 1.0) -> dict:
    """Save the checked preview lines as confirmed, dated prices. Returns a summary."""
    loc = ai.LOCATIONS[location_key]
    session, when = ai.DB_SESSION, _parse_date(document_date)
    updated, created, added_ingredients, skipped = [], [], [], []

    for line in lines:
        name = db.norm_name(line["ingredient"])
        unit = (line.get("unit") or "").strip()
        price = float(line["price_per_unit"])
        if not name or price <= 0:
            skipped.append(f"{line.get('ingredient')}: missing name or price")
            continue
        item = loc.inventory.get(name)
        if item is None:
            if not line.get("add_new"):
                skipped.append(f"{name}: not in the inventory (tick 'add' to create it)")
                continue
            db.upsert_ingredient(session, loc.db_id, {
                "name": name, "unit": unit or "unit", "stock": 0, "reorder_point": 0,
                "reorder_qty": line.get("case_size") or 1, "min_order_qty": 1.0, "shelf_life_days": None,
                "daily_usage_estimate": 0.0, "aliases": name.replace("_", " ")})
            added_ingredients.append(name)
        elif unit and unit != item.unit:
            skipped.append(f"{name}: price is per '{unit}' but the inventory counts '{item.unit}' - convert it first")
            continue

        existing = db.find_supplier_offer(session, supplier_name, name, loc.region)
        base = {c: getattr(existing, c) for c in ("delivery_days", "reliability_score", "delivery_fee",
                                                  "free_shipping_at", "min_order", "notes")} if existing else \
            {"delivery_days": delivery_days, "reliability_score": 0.9, "delivery_fee": 0.0,
             "free_shipping_at": None, "min_order": 1.0, "notes": ""}
        db.upsert_supplier(session, existing.id if existing else None, {
            **base,
            "name": existing.name if existing else supplier_name.strip(),
            "ingredient_name": name,
            "price_per_unit": price,
            "case_size": line.get("case_size") or None,
            "region_code": loc.region,
            "notes": f"price from invoice/price list of {when:%Y-%m-%d}",
            "price_updated_at": when,
        })
        (updated if existing else created).append(name)

    ai.reload_from_db()
    return {"supplier": supplier_name, "date": f"{when:%Y-%m-%d}", "updated": updated, "created": created,
            "added_ingredients": added_ingredients, "skipped": skipped}


def price_age_days(supplier: ai.Supplier) -> Optional[int]:
    if supplier.price_source != "confirmed" or supplier.price_updated_at is None:
        return None
    return (datetime.now() - supplier.price_updated_at).days
