"""Test fixtures. Everything runs offline with no API keys.

The session-scoped `world` fixture seeds a fresh dev-scale world into a
temp directory and points the agent at it through the CARTWHEEL_DB and
CARTWHEEL_POLICIES_DIR env vars, so tests never touch data/. Tests that
mutate the database (refunds, cancellations) use `world_copy`, which hands
each test its own copy.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from seed.generate import generate_world


# Credentials a developer keeps in .env (a shared LLM service, a vendor key,
# the local Langfuse stack) reach these tests through any code path that
# calls load_env, and os.environ keeps them for the rest of the session. A
# stray key changes model routing or sends a test that promises to stay
# offline to a provider or to localhost:3000, so the offline suite runs with
# none of them. The live Module 3 tests override this fixture in
# tests/eval/conftest.py.
SERVICE_ENV_VARS = (
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_JUDGE_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "TOGETHER_API_KEY",
    "GEMINI_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_HOST",
)


@pytest.fixture(autouse=True)
def offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every service credential for the duration of one test."""
    for name in SERVICE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("world")
    db = root / "cartwheel.db"
    policies = root / "policies"
    generate_world(scale="dev", db_path=db, policies_dir=policies)
    os.environ["CARTWHEEL_DB"] = str(db)
    os.environ["CARTWHEEL_POLICIES_DIR"] = str(policies)
    return {"db": db, "policies": policies}


@pytest.fixture
def world_copy(
    world: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    db = tmp_path / "cartwheel.db"
    shutil.copy(world["db"], db)
    monkeypatch.setenv("CARTWHEEL_DB", str(db))
    return db


@pytest.fixture
def analysis_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hand each Module 2 test its own copy of the committed demo state.

    The error-analysis helpers read and write files under
    ``analysis/state/`` (the Artifact J layout). Copying the committed demo
    state into a temp dir and pointing ``CARTWHEEL_ANALYSIS_STATE`` there
    lets the invariant tests exercise writes (freeze, label appends) without
    mutating the checked-in fixture, exactly as ``world_copy`` does for the
    database. Everything here is offline: no keys, no LLM calls.
    """
    src = Path(__file__).resolve().parent.parent / "analysis" / "state"
    dst = tmp_path / "state"
    shutil.copytree(src, dst)
    monkeypatch.setenv("CARTWHEEL_ANALYSIS_STATE", str(dst))
    return dst


@pytest.fixture
def order_search_cases(world_copy):
    """Old matches across independent user/store scopes behind 20 newer orders."""
    from agent import db

    with db.connection() as conn:
        title = "Unique Search Fixture Product"
        matching_products = {}
        ordinary_products = {}
        for store in (1, 2):
            products = db.list_products(conn, store_id=store)
            ordinary_products[store] = products[0].id
            matching_products[store] = products[1].id
            conn.execute("UPDATE products SET title = ? WHERE id = ?", (title, products[1].id))
            conn.execute(
                "UPDATE orders SET product_id = ? WHERE store_id = ?",
                (products[0].id, store),
            )
        ids = [row["id"] for row in conn.execute("SELECT id FROM orders ORDER BY id LIMIT 60")]
        matches = []
        for i, order_id in enumerate(ids):
            user = i % 2 + 1
            store = i // 2 % 2 + 1
            is_match = i < 8
            product = matching_products[store] if is_match else ordinary_products[store]
            ordered_at = "1900-01-01" if is_match else "2099-01-01"
            conn.execute(
                "UPDATE orders SET user_id = ?, store_id = ?, product_id = ?, ordered_at = ? WHERE id = ?",
                (user, store, product, ordered_at, order_id),
            )
            if is_match:
                matches.append((order_id, user, store))
        conn.commit()
        expected = {
            "shopper": [oid for oid, user, store in reversed(matches) if user == 1],
            "merchant": [oid for oid, user, store in reversed(matches) if store == 2],
            "support": [oid for oid, user, store in reversed(matches)],
        }
    return title, expected
