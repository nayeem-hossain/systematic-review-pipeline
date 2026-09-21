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

    # Test pipeline stage endpoints
    res = client.post(f"/api/projects/{project_id}/stages/search?user_id={user_id}")
    assert res.status_code == 200
    assert res.json()["candidates_count"] > 0

    res = client.post(f"/api/projects/{project_id}/stages/dedup?user_id={user_id}")
    assert res.status_code == 200
    assert "unique_candidates" in res.json()

    # Test AI assist prompt & parse reply
    res_p = client.post(f"/api/projects/{project_id}/prompt?user_id={user_id}&phase=1")
    assert res_p.status_code == 200
    assert "prompt" in res_p.json()

    # Test parsing reply
    sample_reply = "1 | INCLUDE | evaluated machine learning method"
    res_parse = client.post(f"/api/projects/{project_id}/parse-reply?user_id={user_id}", json={"reply_text": sample_reply, "phase": 1})
    assert res_parse.status_code == 200

    # Test Review Gate GET & POST
    res_gate_get = client.get(f"/api/projects/{project_id}/review-gate?user_id={user_id}&phase=1")
    assert res_gate_get.status_code == 200
    assert "included_or_maybe" in res_gate_get.json()

    res_gate_post = client.post(f"/api/projects/{project_id}/review-gate?user_id={user_id}", json={"phase": 1, "to_include": ["1"], "to_exclude": []})
    assert res_gate_post.status_code == 200

    # Test Query Expansion GET & POST
    res_qe_get = client.get(f"/api/projects/{project_id}/query-expansion?user_id={user_id}&phase=1")
    assert res_qe_get.status_code == 200
    assert "suggested_terms" in res_qe_get.json()

    res_qe_post = client.post(f"/api/projects/{project_id}/query-expansion?user_id={user_id}", json={"phase": 1, "selected_keywords": ["anomaly"], "extra_keywords": ["classifier"]})
    assert res_qe_post.status_code == 200
    assert "next_query" in res_qe_post.json()

    # Test Consolidation Merge
    res_m = client.post(f"/api/projects/{project_id}/consolidation/merge?user_id={user_id}")
    assert res_m.status_code == 200
    assert "n_merged" in res_m.json()

    # Test Consolidation Full-Text GET & POST
    res_ft_get = client.get(f"/api/projects/{project_id}/consolidation/fulltext?user_id={user_id}")
    assert res_ft_get.status_code == 200
    assert "studies" in res_ft_get.json()

    # Test Consolidation Citation Verification
    res_cv = client.post(f"/api/projects/{project_id}/consolidation/verify-citations?user_id={user_id}", json={"limit": 2})
    assert res_cv.status_code == 200
    assert "n_checked" in res_cv.json()

    # Test Extraction Build & GET & EDIT
    res_ext = client.post(f"/api/projects/{project_id}/consolidation/extract?user_id={user_id}")
    assert res_ext.status_code == 200
    assert "n_rows" in res_ext.json()

    res_ext_get = client.get(f"/api/projects/{project_id}/consolidation/extraction-record?user_id={user_id}")
    assert res_ext_get.status_code == 200

    # Test Figures Generation
    res_fig = client.post(f"/api/projects/{project_id}/consolidation/figures?user_id={user_id}")
    assert res_fig.status_code == 200
    assert "prisma_counts" in res_fig.json()

    # Test Reports & Checklists
    res_mr = client.post(f"/api/projects/{project_id}/consolidation/methods-report?user_id={user_id}")
    assert res_mr.status_code == 200
    assert "paragraph" in res_mr.json()

    res_sa = client.post(f"/api/projects/{project_id}/consolidation/review-self-appraisal?user_id={user_id}")
    assert res_sa.status_code == 200
    assert "checklist_markdown" in res_sa.json()

    res_prov = client.post(f"/api/projects/{project_id}/consolidation/provenance?user_id={user_id}")
    assert res_prov.status_code == 200
    assert "markdown" in res_prov.json()

    # Test Diagnostics & Version Check
    res_diag = client.get(f"/api/projects/{project_id}/consolidation/diagnose?user_id={user_id}")
    assert res_diag.status_code == 200
    assert "phases_funnel" in res_diag.json()

    res_vc = client.get("/api/version-check")
    assert res_vc.status_code == 200
    assert "current_version" in res_vc.json()

    res = client.get(f"/api/projects/{project_id}/stages/fulltext?user_id={user_id}")
    assert res.status_code == 200

    res = client.get(f"/api/projects/{project_id}/stages/appraisal?user_id={user_id}")
    assert res.status_code == 200

    res = client.get(f"/api/projects/{project_id}/stages/prisma?user_id={user_id}")
    assert res.status_code == 200

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
