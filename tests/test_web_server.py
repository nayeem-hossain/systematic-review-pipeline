import uuid
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
from server.main import app
from server.routes import set_runs_dir


@pytest.fixture
def test_client(tmp_path):
    set_runs_dir(tmp_path / "runs_web")
    with TestClient(app) as client:
        yield client


def test_root_endpoint(test_client):
    response = test_client.get("/")
    assert response.status_code == 200


def test_project_crud(test_client):
    user_id = f"test-user-{uuid.uuid4().hex[:6]}"

    # List initially empty
    res = test_client.get(f"/api/projects?user_id={user_id}")
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
    res = test_client.post("/api/projects", json=create_payload)
    assert res.status_code == 200
    project_id = res.json()["project_id"]

    # Verify project in list and created timestamp field name
    res = test_client.get(f"/api/projects?user_id={user_id}")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 1
    assert "created" in data["projects"][0]

    # Delete project
    res = test_client.delete(f"/api/projects/{project_id}?user_id={user_id}")
    assert res.status_code == 200

    # Verify project deleted
    res = test_client.get(f"/api/projects?user_id={user_id}")
    assert res.status_code == 200
    assert res.json()["count"] == 0


def test_project_limit_enforcement(test_client):
    user_id = f"test-user-quota-{uuid.uuid4().hex[:6]}"

    for i in range(5):
        payload = {
            "user_id": user_id,
            "topic": f"Topic {i}",
            "keyword_blocks": [["test"]],
        }
        res = test_client.post("/api/projects", json=payload)
        assert res.status_code == 200

    # 6th attempt should fail with 400
    payload = {
        "user_id": user_id,
        "topic": "Topic 6",
        "keyword_blocks": [["test"]],
    }
    res = test_client.post("/api/projects", json=payload)
    assert res.status_code == 400
    assert "Project limit reached" in res.json()["detail"]


def test_path_traversal_prevention(test_client):
    invalid_user_ids = ["../etc", "..", "user/id", "user\\id", "user@domain", "user id"]
    for bad_id in invalid_user_ids:
        res = test_client.get(f"/api/projects?user_id={bad_id}")
        assert res.status_code == 400
        assert "Invalid" in res.json()["detail"] or "traversal" in res.json()["detail"]
