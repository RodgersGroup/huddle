"""Tests for the shopping list API endpoints."""

from tests.conftest import _set_auth, _make_tenant
from fastapi.testclient import TestClient
from app import app


def test_create_shopping_item(client, seed_households):
    resp = client.post("/api/shopping", json={
        "name": "Milk",
        "quantity": "2L",
        "category": "dairy",
        "notes": "Full cream",
        "added_by": "TestUser",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] is not None
    assert data["message"] == "Item added"


def test_list_shopping_items(client, seed_households):
    client.post("/api/shopping", json={"name": "Bread", "category": "bakery"})
    client.post("/api/shopping", json={"name": "Apples", "category": "fruit_veg"})

    resp = client.get("/api/shopping")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    names = {i["name"] for i in items}
    assert names == {"Bread", "Apples"}


def test_purchase_and_unpurchase(client, seed_households):
    resp = client.post("/api/shopping", json={"name": "Eggs"})
    item_id = resp.json()["id"]

    # Purchase
    resp = client.post(f"/api/shopping/{item_id}/purchase")
    assert resp.status_code == 200

    items = client.get("/api/shopping").json()["items"]
    egg = next(i for i in items if i["id"] == item_id)
    assert egg["purchased"] == 1
    assert egg["purchased_at"] is not None

    # Unpurchase
    resp = client.post(f"/api/shopping/{item_id}/unpurchase")
    assert resp.status_code == 200

    items = client.get("/api/shopping").json()["items"]
    egg = next(i for i in items if i["id"] == item_id)
    assert egg["purchased"] == 0
    assert egg["purchased_at"] is None


def test_update_shopping_item(client, seed_households):
    resp = client.post("/api/shopping", json={"name": "Butter", "category": "dairy"})
    item_id = resp.json()["id"]

    resp = client.put(f"/api/shopping/{item_id}", json={
        "name": "Salted Butter",
        "quantity": "500g",
    })
    assert resp.status_code == 200

    items = client.get("/api/shopping").json()["items"]
    butter = next(i for i in items if i["id"] == item_id)
    assert butter["name"] == "Salted Butter"
    assert butter["quantity"] == "500g"


def test_delete_shopping_item(client, seed_households):
    resp = client.post("/api/shopping", json={"name": "Chips"})
    item_id = resp.json()["id"]

    resp = client.delete(f"/api/shopping/{item_id}")
    assert resp.status_code == 200

    items = client.get("/api/shopping").json()["items"]
    assert all(i["id"] != item_id for i in items)


def test_clear_purchased(client, seed_households):
    client.post("/api/shopping", json={"name": "Item A"})
    resp_b = client.post("/api/shopping", json={"name": "Item B"})
    item_b_id = resp_b.json()["id"]

    # Purchase only Item B
    client.post(f"/api/shopping/{item_b_id}/purchase")

    # Clear purchased
    resp = client.delete("/api/shopping/purchased")
    assert resp.status_code == 200

    items = client.get("/api/shopping").json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Item A"


def test_validation_empty_name(client, seed_households):
    resp = client.post("/api/shopping", json={"name": ""})
    assert resp.status_code == 400

    resp = client.post("/api/shopping", json={"name": "   "})
    assert resp.status_code == 400


def test_household_isolation(seed_households):
    """Shopping items from one household should not be visible to another."""
    t1 = _make_tenant(user_id=1, household_id=1, name="TestUser", role="manager")
    t2 = _make_tenant(user_id=10, household_id=2, name="OtherUser", role="manager")

    _set_auth(t1)
    c = TestClient(app, raise_server_exceptions=False)
    c.post("/api/shopping", json={"name": "H1 Milk"})

    _set_auth(t2)
    c.post("/api/shopping", json={"name": "H2 Bread"})

    # Check H1 sees only its items
    _set_auth(t1)
    items = c.get("/api/shopping").json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "H1 Milk"

    # Check H2 sees only its items
    _set_auth(t2)
    items = c.get("/api/shopping").json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "H2 Bread"


def test_not_found_returns_404(client, seed_households):
    resp = client.delete("/api/shopping/99999")
    assert resp.status_code == 404

    resp = client.post("/api/shopping/99999/purchase")
    assert resp.status_code == 404
