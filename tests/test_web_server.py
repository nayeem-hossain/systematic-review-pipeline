import uuid
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
from server.main import app

client = TestClient(app)

def test_root_endpoint():
    response = client.get("/")
    assert response.status_code == 200

def test_project_crud():
    user_id = f"test-user-{uuid.uuid4().hex[:6]}"

    # List initially empty
    res = client.get(f"/api/projects?user_id={user_id}")
    assert res.status_code == 200
    assert res.json()["count"] == 0

    # Create project
    create_payload = {
        "user_id": user_id,
        "topic": "Test ML Review",
        "keyword_blocks": [["intrusion detection"], ["machine learning"]],
        "inclusion_criteria": "Must evaluate ML",
        "exclusion_criteria": "Non-English",
        "year_from": 2020,
        "year_to": 2026
    }
    res = client.post("/api/projects", json=create_payload)
    assert res.status_code == 200
    project_id = res.json()["project_id"]

    # Verify project in list
    res = client.get(f"/api/projects?user_id={user_id}")
    assert res.status_code == 200
    assert res.json()["count"] == 1

    # Delete project
    res = client.delete(f"/api/projects/{project_id}?user_id={user_id}")
    assert res.status_code == 200

    # Verify project deleted
    res = client.get(f"/api/projects?user_id={user_id}")
    assert res.status_code == 200
    assert res.json()["count"] == 0

def test_project_limit_enforcement():
    user_id = f"test-user-quota-{uuid.uuid4().hex[:6]}"

    for i in range(5):
        payload = {
            "user_id": user_id,
            "topic": f"Topic {i}",
            "keyword_blocks": [["test"]],
        }
        res = client.post("/api/projects", json=payload)
        assert res.status_code == 200

    # 6th attempt should fail with 400
    payload = {
        "user_id": user_id,
        "topic": "Topic 6",
        "keyword_blocks": [["test"]],
    }
    res = client.post("/api/projects", json=payload)
    assert res.status_code == 400
    assert "Project limit reached" in res.json()["detail"]
