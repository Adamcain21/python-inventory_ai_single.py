"""
Create the database for a REAL restaurant: the regional supplier catalog and your
restaurant, with no demo data (no fake stock, no carrot soup).

    python setup_restaurant.py
    python setup_restaurant.py --name "Chez Adam" --key chez_adam --region QC --capacity 120 --budget 3000

An existing database is never deleted: with --force it is renamed to a .bak file first.
"""

import argparse
import os
import shutil
from datetime import datetime

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

import db  # noqa: E402  (after load_dotenv so INVENTORY_DB_URL from .env is used)


def ask(value, question, cast=str):
    while value in (None, ""):
        value = input(question).strip()
    return cast(value)


def main():
    parser = argparse.ArgumentParser(description="Create the database for a real restaurant.")
    parser.add_argument("--name", help='Restaurant name, e.g. "Chez Adam"')
    parser.add_argument("--key", help="Short id without spaces, e.g. chez_adam")
    parser.add_argument("--region", help="QC or ON")
    parser.add_argument("--capacity", type=int, help="Covers per day")
    parser.add_argument("--budget", type=float, help="Weekly purchasing budget ($)")
    parser.add_argument("--force", action="store_true", help="Rename the existing database and start over")
    args = parser.parse_args()

    url = db.default_db_url()
    path = url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else None
    if path and os.path.exists(path):
        if not args.force:
            raise SystemExit(f"A database already exists: {path}\n"
                             f"Run again with --force to keep it as a backup and start a new one.")
        backup = f"{path}.{datetime.now():%Y%m%d-%H%M%S}.bak"
        shutil.move(path, backup)
        print(f"Existing database kept as: {backup}")

    name = ask(args.name, "Restaurant name: ")
    key = ask(args.key or db.norm_name(name), "Short id: ")
    region = ask(args.region, "Region (QC or ON): ").upper()
    capacity = ask(args.capacity, "Covers per day (e.g. 120): ", int)
    budget = ask(args.budget, "Weekly purchasing budget in $ (e.g. 3000): ", float)

    session = db.open_session(url, seed=False)
    try:
        db.create_restaurant_db(session, key, name, region, capacity, budget)
    except db.ConfigError as e:
        raise SystemExit(f"Error: {e}")
    finally:
        session.close()

    print(f"""
Done: "{name}" ({region}) created in {path or url}
Next:
  1. demarrer.bat   (or: python -m uvicorn api:app --host 0.0.0.0 --port 8000)
  2. open http://localhost:8000 -> Configuration: add your ingredients and recipes
     (or tell the AI: "on utilise du saumon, du steak, des frites...")
  3. Prix -> Importer une facture: photograph your last invoices to load your real prices
""")


if __name__ == "__main__":
    main()
