"""Unit tests for parent resolution inside one bulk ingredient upload.

These tests drive the route function directly with a fake database so the
same-batch parent rules are covered without needing a PostgreSQL container.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from models.requests import BulkIngredientUpload
from routes.ingredients import bulk_upload_ingredients

EDITOR = SimpleNamespace(user_id="editor-1")


class FakeBulkIngredientDB:
    """Minimal stand-in for Database covering the bulk upload code path."""

    def __init__(self, existing: dict[str, int] | None = None):
        self.existing = {
            name.casefold(): ingredient_id
            for name, ingredient_id in (existing or {}).items()
        }
        self.created: list[dict[str, Any]] = []
        self._next_id = 100

    def check_ingredient_names_batch(self, names: list[str]) -> dict[str, bool]:
        return {name: name.casefold() in self.existing for name in names}

    def search_ingredients_batch(self, names: list[str]) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for name in names:
            ingredient_id = self.existing.get(name.casefold())
            if ingredient_id is not None:
                results[name] = {"id": ingredient_id, "name": name}
        return results

    def execute_query(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("bulk upload should not issue per-row queries")

    def create_ingredient(self, data: dict[str, Any]) -> dict[str, Any]:
        ingredient_id = self._next_id
        self._next_id += 1
        row = {
            "id": ingredient_id,
            "name": data["name"],
            "description": data.get("description"),
            "parent_id": data.get("parent_id"),
            "path": f"/{data['name']}/",
            "allow_substitution": bool(data.get("allow_substitution")),
            "percent_abv": None,
            "sugar_g_per_l": None,
            "titratable_acidity_g_per_l": None,
            "url": None,
            "created_by": data.get("created_by"),
        }
        self.created.append(row)
        return row


async def _upload(db: FakeBulkIngredientDB, ingredients: list[dict[str, Any]]) -> Any:
    request = BulkIngredientUpload(ingredients=ingredients)
    return await bulk_upload_ingredients(request, db=db, user=EDITOR)


@pytest.mark.asyncio
async def test_parent_defined_earlier_in_batch_resolves():
    db = FakeBulkIngredientDB()

    response = await _upload(
        db,
        [
            {"name": "Cat"},
            {"name": "Brand", "parent_name": "Cat"},
        ],
    )

    assert response.uploaded_count == 2
    assert response.validation_errors == []
    cat = next(i for i in response.uploaded_ingredients if i.name == "Cat")
    brand = next(i for i in response.uploaded_ingredients if i.name == "Brand")
    assert brand.parent_id == cat.id


@pytest.mark.asyncio
async def test_parent_defined_later_in_batch_is_created_first():
    db = FakeBulkIngredientDB()

    response = await _upload(
        db,
        [
            {"name": "Brand", "parent_name": "Cat"},
            {"name": "Cat"},
        ],
    )

    assert response.uploaded_count == 2
    assert response.validation_errors == []
    assert [row["name"] for row in db.created] == ["Cat", "Brand"]
    assert db.created[1]["parent_id"] == db.created[0]["id"]


@pytest.mark.asyncio
async def test_parent_from_database_still_resolves():
    db = FakeBulkIngredientDB({"Vodka": 7})

    response = await _upload(db, [{"name": "Premium Vodka", "parent_name": "Vodka"}])

    assert response.uploaded_count == 1
    assert response.validation_errors == []
    assert response.uploaded_ingredients[0].parent_id == 7


@pytest.mark.asyncio
async def test_unknown_parent_is_still_rejected():
    db = FakeBulkIngredientDB()

    response = await _upload(db, [{"name": "Brand", "parent_name": "Missing"}])

    assert response.uploaded_count == 0
    assert [e.error_type for e in response.validation_errors] == ["parent_not_found"]
    assert db.created == []


@pytest.mark.asyncio
async def test_circular_parent_reference_is_rejected():
    db = FakeBulkIngredientDB()

    response = await _upload(
        db,
        [
            {"name": "A", "parent_name": "B"},
            {"name": "B", "parent_name": "A"},
        ],
    )

    assert response.uploaded_count == 0
    assert {e.error_type for e in response.validation_errors} == {"invalid_parent"}
    assert db.created == []
