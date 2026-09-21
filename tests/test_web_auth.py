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
