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

    # Fetch profile GET endpoint
    res_prof = client.get(f"/api/auth/profile?user_id={user_id}")
    assert res_prof.status_code == 200
    assert res_prof.json()["user"]["email"] == email

    # Invalid password login
    res = client.post("/api/auth/login", json={"email": email, "password": "wrongpassword"})
    assert res.status_code == 401

def test_user_profile_update_and_deletion():
    email = f"testprofile_{uuid.uuid4().hex[:8]}@example.com"
    password = "secretpassword123"

    res = client.post("/api/auth/register", json={"email": email, "password": password})
    user_id = res.json()["user"]["user_id"]

    new_email = f"updated_{uuid.uuid4().hex[:8]}@example.com"
    all_keys = {
        "s2": "k_s2", "pubmed": "k_pm", "core": "k_core", "ieee": "k_ieee",
        "scopus": "k_scopus", "scopus_insttoken": "k_token", "springer": "k_springer", "wos": "k_wos"
    }
    res = client.put("/api/auth/profile", json={
        "user_id": user_id,
        "new_email": new_email,
        "new_password": "newpassword123",
        "api_keys": all_keys
    })
    assert res.status_code == 200
    assert res.json()["user"]["email"] == new_email
    assert res.json()["user"]["api_keys"]["scopus"] == "k_scopus"
    assert res.json()["user"]["api_keys"]["s2"] == "k_s2"

    # Authenticate with new credentials
    res = client.post("/api/auth/login", json={"email": new_email, "password": "newpassword123"})
    assert res.status_code == 200

    # Test API Key endpoint with empty / invalid keys
    res_test = client.post("/api/auth/test-key", json={"service": "scopus", "api_key": ""})
    assert res_test.status_code == 200
    assert res_test.json()["valid"] is False

    res_test2 = client.post("/api/auth/test-key", json={"service": "unknown_svc", "api_key": "test1234"})
    assert res_test2.status_code == 200
    assert res_test2.json()["valid"] is False

    # Delete account
    res = client.delete(f"/api/auth/account?user_id={user_id}")
    assert res.status_code == 200

    # Ensure deleted
    res = client.post("/api/auth/login", json={"email": new_email, "password": "newpassword123"})
    assert res.status_code == 401
