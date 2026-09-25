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


def test_kappa_route_and_maybe_proceeds():
    user_id = f"test-user-kappa-{uuid.uuid4().hex[:6]}"

    # Create project
    create_payload = {
        "user_id": user_id,
        "topic": "Test Kappa Review",
        "keyword_blocks": [["software"], ["testing"]],
    }
    res = client.post("/api/projects", json=create_payload)
    assert res.status_code == 200
    project_id = res.json()["project_id"]

    # Test kappa route
    kappa_payload = {
        "phase": 1,
        "stage_col": "ta_decision",
        "sheet_a_text": "id,ta_decision\n1,include\n2,exclude\n3,maybe\n",
        "sheet_b_text": "id,ta_decision\n1,include\n2,include\n3,maybe\n",
    }
    res_k = client.post(f"/api/projects/{project_id}/consolidation/kappa?user_id={user_id}", json=kappa_payload)
    assert res_k.status_code == 200
    assert "kappa" in res_k.json()
    assert res_k.json()["n_compared"] == 3

    # Test parse-reply with 'maybe' and verify get_fulltext_studies includes 'maybe'
    client.post(f"/api/projects/{project_id}/stages/search?user_id={user_id}")
    client.post(f"/api/projects/{project_id}/stages/dedup?user_id={user_id}")
    sample_reply = "1 | MAYBE | uncertain study"
    client.post(f"/api/projects/{project_id}/parse-reply?user_id={user_id}", json={"reply_text": sample_reply, "phase": 1})

    res_ft = client.get(f"/api/projects/{project_id}/stages/fulltext?user_id={user_id}")
    assert res_ft.status_code == 200
    studies = res_ft.json()["studies"]
    # Study 1 with 'maybe' decision must be included in full-text assessment list
    assert any(str(s.get("id")) == "1" for s in studies)

    # Clean up project
    client.delete(f"/api/projects/{project_id}?user_id={user_id}")


def test_websocket_realtime_connection():
    project_id = f"test-proj-ws-{uuid.uuid4().hex[:6]}"
    with client.websocket_connect(f"/api/ws/{project_id}") as websocket:
        data = websocket.receive_json()
        assert data["type"] == "connected"
        assert data["project_id"] == project_id
        # Send a ping and receive pong
        websocket.send_text('{"type": "ping"}')
        reply = websocket.receive_json()
        assert reply["type"] == "pong"


def test_consolidation_all_actions_and_fulltext_screening():
    user_id = f"test-user-all19-{uuid.uuid4().hex[:6]}"
    create_payload = {
        "user_id": user_id,
        "topic": "Comprehensive Consolidation Test",
        "keyword_blocks": [["systematic"], ["review"]],
    }
    res = client.post("/api/projects", json=create_payload)
    assert res.status_code == 200
    project_id = res.json()["project_id"]

    # Run search & dedup & screen with 2 studies
    client.post(f"/api/projects/{project_id}/stages/search?user_id={user_id}")
    client.post(f"/api/projects/{project_id}/stages/dedup?user_id={user_id}")
    client.post(
        f"/api/projects/{project_id}/parse-reply?user_id={user_id}",
        json={"reply_text": "1 | INCLUDE | study 1 passed\n2 | EXCLUDE | study 2 out", "phase": 1}
    )

    # 1. Merge TA-Included
    res1 = client.post(f"/api/projects/{project_id}/consolidation/merge?user_id={user_id}")
    assert res1.status_code == 200
    assert "n_merged" in res1.json()

    # 2. Citation Snowballing
    res2 = client.post(
        f"/api/projects/{project_id}/consolidation/snowball?user_id={user_id}",
        json={"direction": "both", "max_per_seed": 5}
    )
    assert res2.status_code == 200
    assert "n_found" in res2.json()

    # 3. Merge Snowball Results
    res3 = client.post(f"/api/projects/{project_id}/consolidation/merge-snowball?user_id={user_id}")
    assert res3.status_code == 200
    assert "n_added" in res3.json()

    # 4. Download PDFs
    res4 = client.post(
        f"/api/projects/{project_id}/consolidation/download-pdfs?user_id={user_id}",
        json={"max_downloads": 5}
    )
    assert res4.status_code == 200
    assert "n_downloaded" in res4.json()

    # 5. Full-Text Screening (GET and POST decision)
    res5_get = client.get(f"/api/projects/{project_id}/consolidation/fulltext?user_id={user_id}")
    assert res5_get.status_code == 200
    assert "studies" in res5_get.json()
    if res5_get.json()["studies"]:
        sid = res5_get.json()["studies"][0]["id"]
        res5_post = client.post(
            f"/api/projects/{project_id}/consolidation/fulltext?user_id={user_id}",
            json={"study_id": str(sid), "decision": "exclude", "reason": "Not empirical"}
        )
        assert res5_post.status_code == 200

    # 6. Verify Citations (DOIs)
    res6 = client.post(
        f"/api/projects/{project_id}/consolidation/verify-citations?user_id={user_id}",
        json={"limit": 2}
    )
    assert res6.status_code == 200
    assert "n_checked" in res6.json()

    # 7. Build Extraction Sheet
    res7 = client.post(f"/api/projects/{project_id}/consolidation/extract?user_id={user_id}")
    assert res7.status_code == 200
    assert "n_rows" in res7.json()

    # 8. Edit Extraction Record
    res8_get = client.get(f"/api/projects/{project_id}/consolidation/extraction-record?user_id={user_id}")
    assert res8_get.status_code == 200
    if res8_get.json()["records"]:
        sid = res8_get.json()["records"][0]["id"]
        res8_put = client.put(
            f"/api/projects/{project_id}/consolidation/extraction-record?user_id={user_id}",
            json={"study_id": str(sid), "field": "venue_tier", "value": "A*"}
        )
        assert res8_put.status_code == 200

    # 9. PRISMA & Tier Figures
    res9 = client.post(f"/api/projects/{project_id}/consolidation/figures?user_id={user_id}")
    assert res9.status_code == 200
    assert "prisma_counts" in res9.json()

    # 10. Export References (BibTeX and RIS)
    res10_bib = client.get(f"/api/projects/{project_id}/export/bibtex?user_id={user_id}")
    assert res10_bib.status_code == 200
    assert res10_bib.json()["filename"] == "references.bib"

    res10_ris = client.get(f"/api/projects/{project_id}/export/ris?user_id={user_id}")
    assert res10_ris.status_code == 200
    assert res10_ris.json()["filename"] == "references.ris"

    # 11. Export Full-Text Exclusions
    res11 = client.post(f"/api/projects/{project_id}/consolidation/export-exclusions?user_id={user_id}")
    assert res11.status_code == 200

    # 12. Inter-Rater Agreement (Kappa)
    res12 = client.post(
        f"/api/projects/{project_id}/consolidation/kappa?user_id={user_id}",
        json={
            "phase": 1,
            "stage_col": "ta_decision",
            "sheet_a_text": "id,ta_decision\n1,include\n2,exclude\n",
            "sheet_b_text": "id,ta_decision\n1,include\n2,include\n"
        }
    )
    assert res12.status_code == 200
    assert res12.json()["n_compared"] == 2

    # 13. Draft Methods Paragraph
    res13 = client.post(f"/api/projects/{project_id}/consolidation/methods-report?user_id={user_id}")
    assert res13.status_code == 200
    assert "paragraph" in res13.json()

    # 14. Review Self-Appraisal Checklist
    res14 = client.post(f"/api/projects/{project_id}/consolidation/review-self-appraisal?user_id={user_id}")
    assert res14.status_code == 200
    assert "checklist_markdown" in res14.json()

    # 15. Write Provenance Report
    res15 = client.post(f"/api/projects/{project_id}/consolidation/provenance?user_id={user_id}")
    assert res15.status_code == 200
    assert "markdown" in res15.json()

    # 16. Diagnose Run
    res16 = client.get(f"/api/projects/{project_id}/consolidation/diagnose?user_id={user_id}")
    assert res16.status_code == 200
    assert "phases_funnel" in res16.json()

    # 17. Re-run Phase Search
    res17 = client.post(
        f"/api/projects/{project_id}/consolidation/rerun-search?user_id={user_id}",
        json={"phase": 1, "year_from": 2021, "year_to": 2025}
    )
    assert res17.status_code == 200

    # 18. API Keys test endpoint
    res18 = client.post("/api/auth/test-key", json={"service": "pubmed", "api_key": ""})
    assert res18.status_code == 200

    # 19. Version Check
    res19 = client.get("/api/version-check")
    assert res19.status_code == 200
    assert "current_version" in res19.json()

    # PRISMA stage endpoint verification (Task 3.2)
    res_prisma = client.get(f"/api/projects/{project_id}/stages/prisma?user_id={user_id}")
    assert res_prisma.status_code == 200
    assert "prisma_counts" in res_prisma.json()
    assert "screened" in res_prisma.json()["prisma_counts"]

    # Fulltext stages workflow verification (Task 3.4)
    res_ft_stage = client.get(f"/api/projects/{project_id}/stages/fulltext?user_id={user_id}")
    assert res_ft_stage.status_code == 200

    # Clean up project
    client.delete(f"/api/projects/{project_id}?user_id={user_id}")
