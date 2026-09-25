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


def test_sqlite_db_and_encryption_auth(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    test_enc_key = Fernet.generate_key().decode("utf-8")
    db_file = tmp_path / "test_auth.db"
    db_url = f"sqlite:///{db_file}"

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("APP_ENCRYPTION_KEY", test_enc_key)

    import server.auth as auth_mod
    auth_mod._fernet = Fernet(test_enc_key.encode("utf-8"))
    auth_mod._init_db_schema_if_needed()

    email = f"db_user_{uuid.uuid4().hex[:6]}@example.com"
    pwd = "securepassword123"

    # Register in DB
    reg_res = client.post("/api/auth/register", json={"email": email, "password": pwd})
    assert reg_res.status_code == 200
    uid = reg_res.json()["user"]["user_id"]

    # Profile update with API keys in DB
    api_keys = {"s2": "s2_secret_key_123", "pubmed": "pubmed_secret_key_456"}
    up_res = client.put("/api/auth/profile", json={"user_id": uid, "api_keys": api_keys})
    assert up_res.status_code == 200
    assert up_res.json()["user"]["api_keys"]["s2"] == "s2_secret_key_123"

    # Login via DB
    login_res = client.post("/api/auth/login", json={"email": email, "password": pwd})
    assert login_res.status_code == 200
    assert login_res.json()["user"]["api_keys"]["pubmed"] == "pubmed_secret_key_456"

    # Verify directly from SQLite DB table that stored string is encrypted
    conn = auth_mod.sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("SELECT api_keys FROM users WHERE user_id = ?;", (uid,))
    raw_db_json = cur.fetchone()[0]
    conn.close()

    assert "s2_secret_key_123" not in raw_db_json

    # Delete account from DB
    del_res = client.delete(f"/api/auth/account?user_id={uid}")
    assert del_res.status_code == 200

    # Ensure deleted from DB
    login_res_deleted = client.post("/api/auth/login", json={"email": email, "password": pwd})
    assert login_res_deleted.status_code == 401


def test_auth_full_lifecycle_and_key_persistence_integration():
    """Integration test for Task 4.6 & 4.7:
    Register -> Store API keys -> Log out -> Re-login in fresh session -> Verify keys persist -> Create project -> Execute stage.
    """
    user_email = f"lifecycle_{uuid.uuid4().hex[:6]}@example.com"
    user_pwd = "integrationPassword123"

    # 1. Register
    reg_resp = client.post("/api/auth/register", json={"email": user_email, "password": user_pwd})
    assert reg_resp.status_code == 200
    user_id = reg_resp.json()["user"]["user_id"]

    # 2. Add API Keys to user profile
    keys_payload = {
        "s2": "s2_live_test_key",
        "pubmed": "pubmed_live_test_key",
        "core": "core_live_test_key",
        "ieee": "ieee_live_test_key"
    }
    prof_update = client.put("/api/auth/profile", json={
        "user_id": user_id,
        "api_keys": keys_payload
    })
    assert prof_update.status_code == 200
    assert prof_update.json()["user"]["api_keys"]["core"] == "core_live_test_key"

    # 3. Simulate Logout
    logout_resp = client.post("/api/auth/logout")
    assert logout_resp.status_code == 200

    # 4. Fresh Login (New session)
    fresh_login = client.post("/api/auth/login", json={"email": user_email, "password": user_pwd})
    assert fresh_login.status_code == 200
    logged_in_user = fresh_login.json()["user"]
    # Task 4.7: Verify API key persistence across sessions
    assert logged_in_user["api_keys"]["s2"] == "s2_live_test_key"
    assert logged_in_user["api_keys"]["pubmed"] == "pubmed_live_test_key"
    assert logged_in_user["api_keys"]["core"] == "core_live_test_key"
    assert logged_in_user["api_keys"]["ieee"] == "ieee_live_test_key"

    # 5. Create project under authenticated user
    proj_resp = client.post("/api/projects", json={
        "user_id": user_id,
        "topic": "Lifecycle Integration Project",
        "keyword_blocks": [["nlp"], ["transformers"]],
    })
    assert proj_resp.status_code == 200
    pid = proj_resp.json()["project_id"]

    # 6. Execute search stage
    search_resp = client.post(f"/api/projects/{pid}/stages/search?user_id={user_id}&phase=1")
    assert search_resp.status_code == 200
    assert search_resp.json()["candidates_count"] > 0

    # 7. Clean up
    client.delete(f"/api/projects/{pid}?user_id={user_id}")
    client.delete(f"/api/auth/account?user_id={user_id}")


def test_guest_access_strictly_rejected():
    # Attempting to access project listing as guest or default-user must return 401
    for guest_id in ("default-user", "guest", "", "null", "undefined"):
        res = client.get(f"/api/projects?user_id={guest_id}")
        assert res.status_code == 401
        assert "Authentication required: guest access is disabled" in res.json()["detail"]

    # Attempting to create a project as guest or default-user must return 401
    create_payload = {
        "user_id": "default-user",
        "topic": "Guest Attempt Review",
        "keyword_blocks": [["test"]],
    }
    res_create = client.post("/api/projects", json=create_payload)
    assert res_create.status_code == 401
    assert "Authentication required: guest access is disabled" in res_create.json()["detail"]

    # Attempting to access stage operations with guest identity must return 401
    res_stage = client.post("/api/projects/dummyproj/stages/search?user_id=default-user&phase=1")
    assert res_stage.status_code == 401
    assert "Authentication required: guest access is disabled" in res_stage.json()["detail"]


