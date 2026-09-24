"""
Shared fixtures: every test gets its own brand-new temporary SQLite file,
seeded with the demo data -- the real inventory.db is never touched.
"""

import pytest
from sqlalchemy import func, select

import inventory_ai as ai


def _close_current_db():
    if ai.DB_SESSION is not None:
        ai.DB_SESSION.close()
        ai.DB_SESSION.get_bind().dispose()
        ai.DB_SESSION = None


@pytest.fixture(autouse=True)
def no_real_secrets(monkeypatch):
    """Tests never call the real AI or need the access code, even if a .env file exists."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "INVENTORY_ACCESS_CODE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("INVENTORY_DB_URL", url)   # so the API also uses the temp DB
    monkeypatch.setenv("INVENTORY_CATALOG", "0")  # small fictional supplier set: predictable prices
    return url


@pytest.fixture
def locations(db_url):
    locs = ai.load_from_db(db_url)
    yield locs
    _close_current_db()


@pytest.fixture
def downtown(locations):
    return locations["downtown"]


@pytest.fixture
def uptown(locations):
    return locations["uptown"]


@pytest.fixture
def restart(db_url):
    """Simulates closing and reopening the program: drops everything in
    memory and reloads from the database file with a fresh connection."""
    def _restart():
        _close_current_db()
        return ai.load_from_db(db_url)
    return _restart


@pytest.fixture
def count_rows():
    def _count(model):
        return ai.DB_SESSION.scalar(select(func.count()).select_from(model))
    return _count
