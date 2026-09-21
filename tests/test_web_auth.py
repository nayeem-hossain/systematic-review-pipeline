import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
from server.main import app

client = TestClient(app)

import uuid

def test_user_register_and_login():
    email = f"testuser_{uuid.uuid4().hex[:8]}@example.com"
    password = "secretpassword123"

    # Register
    res = client.post("/api/auth/register", json={"email": email, "password": password})
    assert res.status_code == 200
    user_id = res.json()["user"]["user_id"]
    assert user_id.startswith("usr_")

    # Login
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200
    assert res.json()["user"]["email"] == email

    # Invalid password login
    res = client.post("/api/auth/login", json={"email": email, "password": "wrongpassword"})
    assert res.status_code == 401

def test_user_profile_update_and_deletion():
    email = f"testprofile_{uuid.uuid4().hex[:8]}@example.com"
    password = "secretpassword123"

    res = client.post("/api/auth/register", json={"email": email, "password": password})
    user_id = res.json()["user"]["user_id"]

    new_email = f"updated_{uuid.uuid4().hex[:8]}@example.com"
    res = client.put("/api/auth/profile", json={
        "user_id": user_id,
        "new_email": new_email,
        "new_password": "newpassword123",
        "api_keys": {"scopus": "key123", "wos": "key456"}
    })
    assert res.status_code == 200
    assert res.json()["user"]["email"] == new_email
    assert res.json()["user"]["api_keys"]["scopus"] == "key123"

    # Authenticate with new credentials
    res = client.post("/api/auth/login", json={"email": new_email, "password": "newpassword123"})
    assert res.status_code == 200

    # Delete account
    res = client.delete(f"/api/auth/account?user_id={user_id}")
    assert res.status_code == 200

    # Ensure deleted
    res = client.post("/api/auth/login", json={"email": new_email, "password": "newpassword123"})
    assert res.status_code == 401
