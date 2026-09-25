from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import List, Optional

import httpx
import pandas as pd
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from server.auth import authenticate_user, delete_user_account, get_user_profile, register_user, update_user_profile, _load_users
import io
import json
from slr import (
    _extract_expansion_terms, _merge_included_across_phases,
    _load_search_strategy_rows, _prisma_report_rows, _PHASE_FUNNEL_STAGES
)
from srp.agreement import compare_reviewers
from srp.appraisal import (
    FIELD_PROFILES, INSTRUMENTS, REVIEW_SELF_CHECK,
    compose_appraisal_disclosure, instrument_columns, render_review_self_appraisal
)
from srp.config import ReviewConfig
from srp.decisions import apply_decisions, ta_proceeds_mask, pick_progressed
from srp.export import to_bibtex, to_ris
from srp.llm_assist import build_screening_prompt, compose_criteria, parse_screening_response
from srp.methods_report import render_search_methods, render_search_strategy_table
from srp.prisma import PhaseFrames, derive_prisma_counts_for_run, prisma_residuals
from srp.provenance import Provenance
from srp.quality_tier import compute_quality_tier
from srp.state import RunState, record_key
from srp.update_check import latest_release_version, is_newer
from srp import __version__ as VERSION

router = APIRouter()

BASE_RUNS_DIR = Path("runs_web")
BASE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
MAX_PROJECTS_PER_USER = 5

_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}

    async def connect(self, project_id: str, websocket: WebSocket):
        await websocket.accept()
        if project_id not in self.active_connections:
            self.active_connections[project_id] = []
        self.active_connections[project_id].append(websocket)

    def disconnect(self, project_id: str, websocket: WebSocket):
        if project_id in self.active_connections:
            if websocket in self.active_connections[project_id]:
                self.active_connections[project_id].remove(websocket)
            if not self.active_connections[project_id]:
                del self.active_connections[project_id]

    async def broadcast(self, project_id: str, message: dict):
        if project_id in self.active_connections:
            dead = []
            for connection in self.active_connections[project_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    dead.append(connection)
            for d in dead:
                self.disconnect(project_id, d)


ws_manager = ConnectionManager()


@router.websocket("/ws/{project_id}")
async def websocket_endpoint(websocket: WebSocket, project_id: str):
    await ws_manager.connect(project_id, websocket)
    try:
        await websocket.send_json({"type": "connected", "project_id": project_id, "message": "Real-time updates active."})
        while True:
            data = await websocket.receive_text()
            # Echo or process client ping/event
            try:
                payload = json.loads(data)
                if payload.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except Exception:
                pass
    except WebSocketDisconnect:
        ws_manager.disconnect(project_id, websocket)


def _run_script_subprocess(cmd: list, secret_env: dict | None = None) -> tuple[int, str, str]:
    env = {**os.environ, **secret_env} if secret_env else None
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return res.returncode, res.stdout, res.stderr


def _clean_df_records(df) -> list[dict]:
    if df.empty:
        return []
    # Convert to DataFrame first if it's a Series
    if isinstance(df, pd.Series):
        df = df.to_frame()
    # Fill NaN and convert to list of dicts
    return df.fillna("").to_dict("records")


# --- Schemas ---
class AuthReq(BaseModel):
    email: str
    password: str


class ProjectCreateReq(BaseModel):
    user_id: str
    topic: str
    keyword_blocks: List[List[str]]
    inclusion_criteria: str = ""
    exclusion_criteria: str = ""
    registration_id: str = ""
    research_field: str = "software_engineering"
    year_from: int = 2020
    year_to: int = 2026
    mailto: str = "user@example.com"
    sources: List[str] = ["openalex", "semanticscholar", "crossref", "arxiv", "pubmed", "doaj"]
    max_per_source: int = 40
    n_phases: int = 1
    title_threshold: int = 92
    assist_tool_name: str = "ChatGPT"
    reviewer: str = "web-user"


class ParseReplyReq(BaseModel):
    reply_text: str
    phase: int = 1


class ReviewGateReq(BaseModel):
    phase: int = 1
    to_exclude: List[str] = []
    to_include: List[str] = []


class QueryExpansionReq(BaseModel):
    phase: int = 1
    selected_keywords: List[str] = []
    extra_keywords: List[str] = []


class SnowballReq(BaseModel):
    direction: str = "both"
    max_per_seed: int = 50


class DownloadPdfsReq(BaseModel):
    max_downloads: int = 50


class VerifyCitationsReq(BaseModel):
    limit: int = 10


class ExtractionEditReq(BaseModel):
    study_id: str
    field: str
    value: str


class KappaReq(BaseModel):
    sheet_a_text: Optional[str] = None
    sheet_b_text: Optional[str] = None
    phase: int = 1
    stage_col: str = "ta_decision"


class RerunSearchReq(BaseModel):
    phase: int = 1
    mailto: Optional[str] = None
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    max_per_source: Optional[int] = None
    sources: Optional[List[str]] = None


class ProfileUpdateReq(BaseModel):
    user_id: str
    new_email: Optional[str] = None
    new_password: Optional[str] = None
    api_keys: Optional[dict[str, str]] = None


class TestKeyReq(BaseModel):
    service: str
    api_key: str
    insttoken: Optional[str] = None


class FullTextDecisionReq(BaseModel):
    phase: int = 1
    study_id: str
    decision: str  # "include" or "exclude"
    reason: str = ""


class AppraisalReq(BaseModel):
    study_id: str
    scores: dict[str, str]


# --- Helpers ---
def _require_profile_holder(user_id: str) -> None:
    if not user_id or user_id in ("default-user", "guest", "null", "undefined"):
        raise HTTPException(
            status_code=401,
            detail="Authentication required: guest access is disabled. Only registered profile holders can access workspace and project operations."
        )


def _get_user_run_dir(user_id: str, project_id: str) -> Path:
    _require_profile_holder(user_id)
    user_dir = BASE_RUNS_DIR / user_id
    user_dir.mkdir(exist_ok=True)
    pdir = user_dir / project_id
    if not pdir.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    return pdir


def _count_user_projects(user_id: str) -> int:
    user_dir = BASE_RUNS_DIR / user_id
    if not user_dir.exists():
        return 0
    return len([d for d in user_dir.iterdir() if d.is_dir() and (d / "config.json").exists()])


def _get_user_api_keys(user_id: str) -> dict[str, str]:
    users = _load_users()
    for _, udata in users.items():
        if udata.get("user_id") == user_id:
            return udata.get("api_keys", {})
    return {}


def _build_secret_env_for_user(user_id: str) -> dict[str, str]:
    keys = _get_user_api_keys(user_id)
    env = {}
    if keys.get("s2"): env["S2_API_KEY"] = keys["s2"]
    if keys.get("pubmed"): env["PUBMED_API_KEY"] = keys["pubmed"]
    if keys.get("core"): env["CORE_API_KEY"] = keys["core"]
    if keys.get("ieee"): env["IEEE_API_KEY"] = keys["ieee"]
    if keys.get("scopus"): env["SCOPUS_API_KEY"] = keys["scopus"]
    if keys.get("scopus_insttoken"): env["SCOPUS_INSTTOKEN"] = keys["scopus_insttoken"]
    if keys.get("springer"): env["SPRINGER_API_KEY"] = keys["springer"]
    if keys.get("wos"): env["WOS_API_KEY"] = keys["wos"]
    return env


def _test_api_key(service: str, api_key: str, insttoken: str = "") -> tuple[bool, str]:
    if not api_key or not api_key.strip():
        return False, "API key is empty."

    key = api_key.strip()
    s_clean = service.strip().lower()

    try:
        with httpx.Client(timeout=10.0) as client:
            if s_clean in ("semanticscholar", "s2"):
                r = client.get("https://api.semanticscholar.org/graph/v1/paper/search?query=test&limit=1", headers={"x-api-key": key})
                if r.status_code == 200:
                    return True, "Semantic Scholar API key is valid."
                return False, f"Semantic Scholar HTTP {r.status_code}: {r.text[:200]}"

            elif s_clean == "pubmed":
                r = client.get(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=test&retmax=1&retmode=json&api_key={key}")
                if r.status_code == 200:
                    return True, "PubMed API key is valid."
                return False, f"PubMed HTTP {r.status_code}: {r.text[:200]}"

            elif s_clean == "core":
                r = client.get("https://api.core.ac.uk/v3/search/works?q=test&limit=1", headers={"Authorization": f"Bearer {key}"})
                if r.status_code == 200:
                    return True, "CORE API key is valid."
                return False, f"CORE HTTP {r.status_code}: {r.text[:200]}"

            elif s_clean == "ieee":
                r = client.get(f"https://ieeexploreapi.ieee.org/api/v1/search/articles?article_number=1&apiKey={key}")
                if r.status_code == 200:
                    return True, "IEEE Xplore API key is valid."
                return False, f"IEEE HTTP {r.status_code}: {r.text[:200]}"

            elif s_clean in ("scopus", "scopus_insttoken"):
                headers = {"X-ELS-APIKey": key, "Accept": "application/json"}
                if insttoken:
                    headers["X-ELS-Insttoken"] = insttoken.strip()
                r = client.get("https://api.elsevier.com/content/search/scopus?query=title(test)&count=1", headers=headers)
                if r.status_code == 200:
                    return True, "Scopus API key / token is valid."
                return False, f"Scopus HTTP {r.status_code}: {r.text[:200]}"

            elif s_clean == "springer":
                r = client.get(f"https://api.springernature.com/meta/v1/json?q=test&p=1&api_key={key}")
                if r.status_code == 200:
                    return True, "Springer API key is valid."
                return False, f"Springer HTTP {r.status_code}: {r.text[:200]}"

            elif s_clean in ("wos", "webofscience"):
                headers = {"X-ApiKey": key}
                r = client.get("https://api.clarivate.com/api/wos?databaseId=WOS&usrQuery=TS=test&count=1&firstRecord=1", headers=headers)
                if r.status_code in (200, 201):
                    return True, "Web of Science API key is valid."
                return False, f"Web of Science HTTP {r.status_code}: {r.text[:200]}"

            else:
                return False, f"Unknown API service '{service}'."
    except Exception as e:
        return False, f"Request failed: {str(e)}"


# --- Auth Endpoints ---

@router.post("/auth/register")
def api_register(req: AuthReq):
    try:
        user_info = register_user(req.email, req.password)
        return {"status": "success", "user": user_info}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/auth/login")
def api_login(req: AuthReq):
    user_info = authenticate_user(req.email, req.password)
    if not user_info:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return {"status": "success", "user": user_info}


@router.get("/auth/profile")
def api_get_profile(user_id: str):
    user_info = get_user_profile(user_id)
    if not user_info:
        raise HTTPException(status_code=404, detail="User profile not found")
    return {"status": "success", "user": user_info}


@router.put("/auth/profile")
def api_update_profile(req: ProfileUpdateReq):
    try:
        updated = update_user_profile(
            user_id=req.user_id,
            new_email=req.new_email,
            new_password=req.new_password,
            api_keys=req.api_keys,
        )
        return {"status": "success", "user": updated}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/auth/test-key")
def api_test_key(req: TestKeyReq):
    success, message = _test_api_key(req.service, req.api_key, req.insttoken or "")
    return {"status": "success", "valid": success, "message": message, "service": req.service}


@router.delete("/auth/account")
def api_delete_account(user_id: str):
    success = delete_user_account(user_id)
    if not success:
        raise HTTPException(status_code=404, detail="User account not found")

    user_dir = BASE_RUNS_DIR / user_id
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)

    return {"status": "success", "message": "Account and associated data deleted"}
@router.post("/auth/logout")
def api_logout():
    return {"status": "success", "message": "Logged out successfully"}


# --- Project Endpoints ---

@router.get("/projects")
def list_projects(user_id: str):
    _require_profile_holder(user_id)
    user_dir = BASE_RUNS_DIR / user_id
    if not user_dir.exists():
        return {"status": "success", "projects": [], "count": 0, "max_allowed": MAX_PROJECTS_PER_USER}

    projects = []
    for pdir in user_dir.iterdir():
        if pdir.is_dir() and (pdir / "config.json").exists():
            state = RunState.load(pdir)
            cfg = state.config
            projects.append({
                "project_id": pdir.name,
                "topic": cfg.get("topic"),
                "n_phases": cfg.get("n_phases"),
                "current_phase": state.state.get("current_phase", 1),
                "created_at": state.state.get("created_at"),
            })
    return {"status": "success", "projects": projects, "count": len(projects), "max_allowed": MAX_PROJECTS_PER_USER}


@router.post("/projects")
def create_project(req: ProjectCreateReq):
    _require_profile_holder(req.user_id)
    current_count = _count_user_projects(req.user_id)
    if current_count >= MAX_PROJECTS_PER_USER:
        raise HTTPException(
            status_code=400,
            detail=f"Project limit reached. Maximum {MAX_PROJECTS_PER_USER} projects allowed per account. Please delete or clear an existing project to free up space."
        )

    project_id = str(uuid.uuid4())[:8]
    user_dir = BASE_RUNS_DIR / req.user_id
    user_dir.mkdir(exist_ok=True)

    profile = FIELD_PROFILES.get(req.research_field, FIELD_PROFILES["software_engineering"])
    appraisal_just = compose_appraisal_disclosure(
        req.research_field, list(profile.primary_study_instruments),
        profile.certainty_framework, profile.review_level_instrument
    )

    cfg = ReviewConfig(
        topic=req.topic,
        keyword_blocks=req.keyword_blocks,
        year_from=req.year_from,
        year_to=req.year_to,
        mailto=req.mailto,
        sources=req.sources,
        max_per_source=req.max_per_source,
        n_phases=req.n_phases,
        title_threshold=req.title_threshold,
        assist_tool_name=req.assist_tool_name,
        reviewer=req.reviewer,
        inclusion_criteria=req.inclusion_criteria,
        exclusion_criteria=req.exclusion_criteria,
        registration_id=req.registration_id,
        research_field=req.research_field,
        primary_study_instruments=list(profile.primary_study_instruments),
        certainty_framework=profile.certainty_framework,
        review_level_instrument=profile.review_level_instrument,
        appraisal_justification=appraisal_just,
    )

    state = RunState.create(user_dir, project_id, cfg.to_dict())
    prov = Provenance(state.run_dir / "provenance.jsonl")
    prov.log("review_created", run_id=project_id, topic=cfg.topic, keywords=cfg.display_keywords())

    return {"status": "success", "project_id": project_id, "message": "Project created successfully"}


@router.delete("/projects/{project_id}")
def delete_project(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    shutil.rmtree(pdir)
    return {"status": "success", "message": f"Project {project_id} deleted successfully"}


@router.get("/projects/{project_id}/status")
def get_project_status(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    return {
        "status": "success",
        "config": state.config,
        "state": state.state,
    }


@router.post("/projects/{project_id}/prompt")
def get_ai_assist_prompt(user_id: str, project_id: str, phase: int = 1):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)
    ph_dir = state.phase_dir(phase)

    dedup_path = ph_dir / "candidates_dedup.csv"
    screening_path = ph_dir / "screening.csv"

    if not dedup_path.exists():
        raise HTTPException(status_code=400, detail="Candidates dedup file not found. Run search first.")

    dedup_df = pd.read_csv(dedup_path)
    if "duplicate_of" in dedup_df.columns:
        dedup_df = dedup_df[dedup_df["duplicate_of"].isna()]

    decided_ids = set()
    if screening_path.exists():
        sc_df = pd.read_csv(screening_path)
        if "ta_decision" in sc_df.columns:
            mask = sc_df["ta_decision"].notna() & (sc_df["ta_decision"].astype(str).str.strip() != "")
            decided_ids = {str(v) for v in sc_df.loc[mask, "id"]}

    undecided = [
        row.to_dict() for _, row in dedup_df.iterrows()
        if str(row.get("id")) not in decided_ids
    ]

    batch = undecided[:20]
    if not batch:
        return {"status": "success", "prompt": None, "message": "No undecided candidates remaining."}

    criteria = compose_criteria("ta", cfg.inclusion_criteria, cfg.exclusion_criteria)
    records = [
        {"id": r.get("id"), "title": r.get("title", ""), "abstract": r.get("abstract", ""),
         "year": r.get("year", ""), "venue": r.get("venue", "")}
        for r in batch
    ]
    prompt = build_screening_prompt(records, stage="ta", topic=cfg.topic, criteria=criteria)
    return {
        "status": "success",
        "prompt": prompt,
        "batch_size": len(batch),
        "total_undecided": len(undecided),
    }


@router.post("/projects/{project_id}/parse-reply")
def parse_ai_assist_reply(user_id: str, project_id: str, req: ParseReplyReq):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    ph_dir = state.phase_dir(req.phase)
    screening_path = ph_dir / "screening.csv"

    if not screening_path.exists():
        raise HTTPException(status_code=400, detail="Screening file not found.")

    parsed = parse_screening_response(req.reply_text)
    applied = apply_decisions(screening_path, parsed, state, req.phase, stage="ta")

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("assist_response_parsed_web", phase=req.phase, n_decided=len(applied.matched), counts=applied.counts)

    return {
        "status": "success",
        "applied_counts": applied.counts,
        "matched_ids": applied.matched,
        "problems": applied.problems(),
    }


@router.get("/projects/{project_id}/review-gate")
def get_review_gate_data(user_id: str, project_id: str, phase: int = 1):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    ph_dir = state.phase_dir(phase)
    screening_path = ph_dir / "screening.csv"

    if not screening_path.exists():
        return {
            "status": "success",
            "included_or_maybe": [],
            "excluded": [],
            "undecided": [],
            "total_records": 0,
        }

    df = pd.read_csv(screening_path)
    if df.empty or "ta_decision" not in df.columns:
        return {
            "status": "success",
            "included_or_maybe": [],
            "excluded": [],
            "undecided": [],
            "total_records": 0,
        }

    decisions = df["ta_decision"].astype(str).str.strip().str.lower()
    # Convert to Series for ta_proceeds_mask
    decisions_series = pd.Series(decisions)
    proceed_mask = ta_proceeds_mask(decisions_series)
    excluded_mask = decisions.eq("exclude")
    # Fix undecided mask
    undecided_mask = df["ta_decision"].isna() | (df["ta_decision"].astype(str).str.strip() == "")

    included_df = df[proceed_mask]
    excluded_df = df[excluded_mask]
    undecided_df = df[undecided_mask]

    return {
        "status": "success",
        "included_or_maybe": _clean_df_records(included_df),
        "excluded": _clean_df_records(excluded_df),
        "undecided": _clean_df_records(undecided_df),
        "total_records": len(df),
    }


@router.post("/projects/{project_id}/review-gate")
def apply_review_gate(user_id: str, project_id: str, req: ReviewGateReq):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)
    ph_dir = state.phase_dir(req.phase)
    screening_path = ph_dir / "screening.csv"

    if not screening_path.exists():
        raise HTTPException(status_code=400, detail="Screening file not found.")

    df = pd.read_csv(screening_path)
    if "reviewer" not in df.columns:
        df["reviewer"] = ""

    to_excl_set = {str(x).strip() for x in req.to_exclude}
    to_inc_set = {str(x).strip() for x in req.to_include}

    n_overridden = 0
    for idx, row in df.iterrows():
        rid = str(row.get("id")).strip()
        new_dec = None
        if rid in to_excl_set:
            new_dec = "exclude"
        elif rid in to_inc_set:
            new_dec = "include"

        if new_dec:
            df.at[idx, "ta_decision"] = new_dec
            prior_reason = str(df.at[idx, "ta_reason"] or "").strip()
            df.at[idx, "ta_reason"] = f"{prior_reason} [reviewer override]".strip()
            df.at[idx, "reviewer"] = cfg.reviewer or "human-review"
            n_overridden += 1

            rk = record_key(str(row.get("doi", "")), str(row.get("title", "")))
            state.record_decision(
                record_key=rk,
                id=str(row.get("id")),
                decision=new_dec,
                stage="ta",
                phase=req.phase,
                reason="reviewer override",
                source="human-review",
                title=str(row.get("title", "")),
                doi=str(row.get("doi", "")),
            )

    df.to_csv(screening_path, index=False, encoding="utf-8")

    decisions = df["ta_decision"].astype(str).str.strip().str.lower() if "ta_decision" in df.columns else pd.Series()
    n_included = int(ta_proceeds_mask(decisions).sum())

    state.mark_stage(req.phase, "review_gate", counts={"n_included": n_included, "n_overridden": n_overridden})
    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("phase_review", phase=req.phase, n_included=n_included, n_overridden=n_overridden)

    return {"status": "success", "message": "Review gate updated successfully", "n_included": n_included, "n_overridden": n_overridden}


@router.get("/projects/{project_id}/query-expansion")
def get_query_expansion_suggestions(user_id: str, project_id: str, phase: int = 1):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)
    ph_dir = state.phase_dir(phase)
    screening_path = ph_dir / "screening.csv"

    titles = []
    if screening_path.exists():
        screening_df = pd.read_csv(screening_path)
        if not screening_df.empty and "ta_decision" in screening_df.columns:
            proceed_mask = ta_proceeds_mask(screening_df["ta_decision"])
            titles = screening_df.loc[proceed_mask, "title"].dropna().astype(str).tolist()

    suggested_terms = _extract_expansion_terms(titles, cfg.all_keywords())
    return {
        "status": "success",
        "phase": phase,
        "included_titles_count": len(titles),
        "suggested_terms": suggested_terms,
        "current_keywords": cfg.display_keywords(),
    }


@router.post("/projects/{project_id}/query-expansion")
def apply_query_expansion(user_id: str, project_id: str, req: QueryExpansionReq):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)

    added_terms = list(req.selected_keywords) + [t.strip() for t in req.extra_keywords if t.strip()]

    if cfg.keyword_blocks:
        new_blocks = list(cfg.keyword_blocks) + [[t] for t in added_terms]
        next_cfg = ReviewConfig.from_dict({**cfg.to_dict(), "keyword_blocks": new_blocks})
    else:
        new_kws = list(cfg.keywords) + added_terms
        next_cfg = ReviewConfig.from_dict({**cfg.to_dict(), "keywords": new_kws})

    next_query = next_cfg.search_query()
    next_phase = req.phase + 1
    state.state[f"phase_{next_phase}_query"] = next_query
    state.mark_stage(req.phase, "snowball", counts={"n_added": len(added_terms)})

    if state.state.get("current_phase", 1) <= req.phase:
        state.state["current_phase"] = next_phase
    state.save()

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("query_expansion", from_phase=req.phase, added_keywords=added_terms)

    return {
        "status": "success",
        "message": f"Query expansion applied for phase {next_phase}",
        "next_phase": next_phase,
        "next_query": next_query,
        "added_terms": added_terms,
    }


# --- Consolidation Menu Endpoints ---

@router.post("/projects/{project_id}/consolidation/merge")
def consolidation_merge_included(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)

    merged = _merge_included_across_phases(state, cfg)
    included_path = pdir / "included_final.csv"
    final_dedup_path = pdir / "final_dedup.csv"

    merged.to_csv(included_path, index=False, encoding="utf-8")

    dedup_cols = ["id", "doi", "url", "title", "authors", "year", "venue"]
    present = [c for c in dedup_cols if c in merged.columns]
    merged[present].to_csv(final_dedup_path, index=False, encoding="utf-8")

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("merge_included", n_included=len(merged))

    return {
        "status": "success",
        "n_merged": len(merged),
        "included_studies": _clean_df_records(merged),
    }


@router.post("/projects/{project_id}/consolidation/snowball")
def consolidation_snowball(user_id: str, project_id: str, req: SnowballReq):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)

    # Ensure merged set exists first
    final_dedup_path = pdir / "final_dedup.csv"
    if not final_dedup_path.exists():
        consolidation_merge_included(user_id, project_id)

    snowball_dir = pdir / "snowball"
    snowball_dir.mkdir(exist_ok=True)
    candidates_path = snowball_dir / "candidates.csv"

    cmd = [
        sys.executable, str(_SCRIPT_DIR / "snowball.py"),
        "--seeds", str(final_dedup_path),
        "--mailto", cfg.mailto,
        "--direction", req.direction,
        "--max-per-seed", str(req.max_per_seed),
        "--out", str(candidates_path),
    ]

    secret_env = _build_secret_env_for_user(user_id)
    code, stdout, stderr = _run_script_subprocess(cmd, secret_env=secret_env)
    if code != 0:
        raise HTTPException(status_code=500, detail=f"Snowball script failed: {stderr}")

    cand_df = pd.read_csv(candidates_path) if candidates_path.exists() else pd.DataFrame()
    n_found = len(cand_df)

    dedup_path = snowball_dir / "candidates_dedup.csv"
    cmd_dedup = [
        sys.executable, str(_SCRIPT_DIR / "dedup.py"),
        "--in", str(candidates_path),
        "--out", str(dedup_path),
        "--title-threshold", str(cfg.title_threshold),
    ]
    dedup_code, dedup_stdout, dedup_stderr = _run_script_subprocess(cmd_dedup)
    if dedup_code != 0:
        raise HTTPException(status_code=500, detail=f"Dedup script failed: {dedup_stderr}")

    screening_path = snowball_dir / "screening.csv"
    cmd_screen = [
        sys.executable, str(_SCRIPT_DIR / "screen.py"),
        "--in", str(dedup_path),
        "--out", str(screening_path),
    ]
    screen_code, screen_stdout, screen_stderr = _run_script_subprocess(cmd_screen)
    if screen_code != 0:
        raise HTTPException(status_code=500, detail=f"Screen script failed: {screen_stderr}")

    dedup_df = pd.read_csv(dedup_path) if dedup_path.exists() else pd.DataFrame()
    screening_df = pd.read_csv(screening_path) if screening_path.exists() else pd.DataFrame()

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("citation_snowball", direction=req.direction, n_found=n_found, n_unique=len(dedup_df))

    return {
        "status": "success",
        "n_found": n_found,
        "n_unique": len(dedup_df),
        "candidates": _clean_df_records(screening_df),
    }


@router.post("/projects/{project_id}/consolidation/merge-snowball")
def consolidation_merge_snowball(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    snowball_screening = pdir / "snowball" / "screening.csv"

    if not snowball_screening.exists():
        raise HTTPException(status_code=400, detail="No snowball screening sheet found -- run snowballing first.")

    screening_df = pd.read_csv(snowball_screening)
    if screening_df.empty or "ta_decision" not in screening_df.columns:
        raise HTTPException(status_code=400, detail="Snowball screening sheet is empty or invalid.")

    ta_series = screening_df["ta_decision"].fillna("").astype(str)
    proceed = screening_df[ta_proceeds_mask(ta_series)].copy()
    if proceed.empty:
        return {"status": "warning", "message": "No included/maybe rows in snowball screening sheet.", "n_added": 0}

    included_path = pdir / "included_final.csv"
    if not included_path.exists():
        consolidation_merge_included(user_id, project_id)

    existing = pd.read_csv(included_path)
    proceed["id"] = [f"sb_{i}" for i in proceed["id"]]
    proceed["phase"] = "snowball"

    for col in ("ft_decision", "ft_reason"):
        if col not in proceed.columns:
            proceed[col] = ""

    existing_keys = set(existing.apply(lambda r: record_key(str(r.get("doi", "")), str(r.get("title", ""))), axis=1))
    proceed["record_key"] = proceed.apply(lambda r: record_key(str(r.get("doi", "")), str(r.get("title", ""))), axis=1)

    new_rows = proceed[~proceed["record_key"].isin(existing_keys)].drop(columns=["record_key"])
    n_dupe = len(proceed) - len(new_rows)

    if not new_rows.empty:
        common_cols = [c for c in existing.columns if c in new_rows.columns]
        combined = pd.concat([existing, new_rows[common_cols]], ignore_index=True)
        combined.to_csv(included_path, index=False, encoding="utf-8")

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("merge_citation_snowball", n_added=len(new_rows), n_duplicate=n_dupe)

    return {
        "status": "success",
        "n_added": len(new_rows),
        "n_duplicate": n_dupe,
    }


@router.post("/projects/{project_id}/consolidation/download-pdfs")
def consolidation_download_pdfs(user_id: str, project_id: str, req: DownloadPdfsReq):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)

    final_dedup_path = pdir / "final_dedup.csv"
    if not final_dedup_path.exists():
        consolidation_merge_included(user_id, project_id)

    outdir = pdir / "pdfs"
    report_path = pdir / "manual_download_needed.csv"
    log_path = pdir / "download_log.csv"

    cmd = [
        sys.executable, str(_SCRIPT_DIR / "download.py"),
        "--in", str(final_dedup_path),
        "--mailto", cfg.mailto,
        "--outdir", str(outdir),
        "--report", str(report_path),
        "--log", str(log_path),
        "--max-downloads", str(req.max_downloads),
    ]

    secret_env = _build_secret_env_for_user(user_id)
    code, stdout, stderr = _run_script_subprocess(cmd, secret_env=secret_env)

    manual_df = pd.read_csv(report_path) if report_path.exists() else pd.DataFrame()
    total_df = pd.read_csv(final_dedup_path) if final_dedup_path.exists() else pd.DataFrame()

    n_manual = len(manual_df)
    n_downloaded = max(len(total_df) - n_manual, 0)

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("download_pdfs", n_downloaded=n_downloaded, n_manual=n_manual)

    return {
        "status": "success",
        "n_downloaded": n_downloaded,
        "n_manual_needed": n_manual,
        "manual_needed": _clean_df_records(manual_df),
    }


@router.get("/projects/{project_id}/consolidation/fulltext")
def get_consolidation_fulltext(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    included_path = pdir / "included_final.csv"

    if not included_path.exists():
        consolidation_merge_included(user_id, project_id)

    df = pd.read_csv(included_path)
    if "ft_decision" not in df.columns:
        df["ft_decision"] = ""
        df["ft_reason"] = ""

    ft = df["ft_decision"].astype(str).str.strip().str.lower()
    n_inc = int(ft.eq("include").sum())
    n_exc = int(ft.eq("exclude").sum())
    n_undecided = len(df) - n_inc - n_exc

    return {
        "status": "success",
        "studies": _clean_df_records(df),
        "n_included": n_inc,
        "n_excluded": n_exc,
        "n_undecided": n_undecided,
        "total": len(df),
    }


@router.post("/projects/{project_id}/consolidation/fulltext")
def record_consolidation_fulltext_decision(user_id: str, project_id: str, req: FullTextDecisionReq):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    included_path = pdir / "included_final.csv"

    if not included_path.exists():
        consolidation_merge_included(user_id, project_id)

    df = pd.read_csv(included_path)
    idx_match = df[df["id"].astype(str) == str(req.study_id)].index
    if len(idx_match) == 0:
        raise HTTPException(status_code=404, detail="Study not found in included_final.csv")

    idx = idx_match[0]
    dec = req.decision.lower().strip()
    reason = req.reason.strip()

    if dec == "exclude" and not reason:
        raise HTTPException(status_code=400, detail="A reason is required for full-text exclusion (PRISMA 16b).")

    if "ft_decision" not in df.columns:
        df["ft_decision"] = ""
    else:
        df["ft_decision"] = df["ft_decision"].astype(object)

    if "ft_reason" not in df.columns:
        df["ft_reason"] = ""
    else:
        df["ft_reason"] = df["ft_reason"].astype(object)

    df.at[idx, "ft_decision"] = dec
    df.at[idx, "ft_reason"] = reason
    df.to_csv(included_path, index=False, encoding="utf-8")

    row = df.loc[idx]
    rk = record_key(str(row.get("doi", "")), str(row.get("title", "")))
    state.record_decision(
        record_key=rk, id=str(row.get("id")), decision=dec, stage="ft", phase=0,
        reason=reason, source="human-review", title=str(row.get("title", "")), doi=str(row.get("doi", ""))
    )

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("full_text_screening_single", study_id=req.study_id, decision=dec, reason=reason)

    return {"status": "success", "message": "Full-text decision recorded"}


@router.post("/projects/{project_id}/consolidation/verify-citations")
def consolidation_verify_citations(user_id: str, project_id: str, req: VerifyCitationsReq):
    pdir = _get_user_run_dir(user_id, project_id)
    included_path = pdir / "included_final.csv"

    if not included_path.exists():
        consolidation_merge_included(user_id, project_id)

    out_path = pdir / "citation_verification.csv"
    cmd = [
        sys.executable, str(_SCRIPT_DIR / "verify_citations.py"),
        "--csv", str(included_path),
        "--out", str(out_path),
        "--limit", str(req.limit),
    ]

    _run_script_subprocess(cmd)

    results_df = pd.read_csv(out_path) if out_path.exists() else pd.DataFrame()
    statuses = results_df["status"].astype(str).str.strip().str.upper() if "status" in results_df.columns else pd.Series()
    n_failed = int(statuses.eq("FAIL").sum())

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("citation_verification", status="ran", n_checked=len(results_df), n_failed=n_failed)

    return {
        "status": "success",
        "n_checked": len(results_df),
        "n_failed": n_failed,
        "results": _clean_df_records(results_df),
    }


@router.post("/projects/{project_id}/consolidation/extract")
def consolidation_build_extraction(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)

    included_path = pdir / "included_final.csv"
    if not included_path.exists():
        consolidation_merge_included(user_id, project_id)

    final_dedup_path = pdir / "final_dedup.csv"
    out_path = pdir / "extraction.csv"

    cmd = [
        sys.executable, str(_SCRIPT_DIR / "extract.py"),
        "--in", str(included_path),
        "--out", str(out_path),
        "--candidates", str(final_dedup_path),
    ]
    if cfg.primary_study_instruments:
        cmd += ["--instruments", ",".join(cfg.primary_study_instruments)]

    _run_script_subprocess(cmd)

    ext_df = pd.read_csv(out_path) if out_path.exists() else pd.DataFrame()
    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("build_extraction_sheet", n_rows=len(ext_df))

    return {
        "status": "success",
        "n_rows": len(ext_df),
        "extraction_records": _clean_df_records(ext_df),
    }


@router.get("/projects/{project_id}/consolidation/extraction-record")
def get_extraction_records(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    ext_path = pdir / "extraction.csv"

    if not ext_path.exists():
        consolidation_build_extraction(user_id, project_id)

    df = pd.read_csv(ext_path) if ext_path.exists() else pd.DataFrame()
    return {"status": "success", "records": _clean_df_records(df), "count": len(df)}


@router.put("/projects/{project_id}/consolidation/extraction-record")
def update_extraction_record(user_id: str, project_id: str, req: ExtractionEditReq):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)
    ext_path = pdir / "extraction.csv"

    if not ext_path.exists():
        consolidation_build_extraction(user_id, project_id)

    df = pd.read_csv(ext_path)
    idx_match = df[df["id"].astype(str) == str(req.study_id)].index
    if len(idx_match) == 0:
        raise HTTPException(status_code=404, detail=f"Study with id '{req.study_id}' not found in extraction.csv")

    idx = idx_match[0]
    field = req.field.strip()
    val = req.value.strip()

    if field not in df.columns:
        df[field] = ""

    old_val = str(df.at[idx, field] or "")
    df.at[idx, field] = val

    # Handle quality tier recalculation or manual override logging
    if field == "quality_tier":
        r, a, t, c = df.at[idx, "R"], df.at[idx, "A"], df.at[idx, "T"], df.at[idx, "C"]
        computed = compute_quality_tier(r, a, t, c)
        if computed is not None and val.upper() != computed:
            prov = Provenance(pdir / "provenance.jsonl")
            prov.log("quality_tier_manual_override", study_id=req.study_id, computed=computed, override=val, reviewer=cfg.reviewer)
    elif field in ("R", "A", "T", "C"):
        r = df.at[idx, "R"] if field != "R" else val
        a = df.at[idx, "A"] if field != "A" else val
        t = df.at[idx, "T"] if field != "T" else val
        c = df.at[idx, "C"] if field != "C" else val
        computed = compute_quality_tier(r, a, t, c)
        if computed is not None:
            df.at[idx, "quality_tier"] = computed

    df.at[idx, "extraction_reviewer"] = cfg.reviewer or "web-user"
    df.at[idx, "extraction_date"] = pd.Timestamp.now().strftime("%Y-%m-%d")
    df.to_csv(ext_path, index=False, encoding="utf-8")

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("extraction_field_edited", study_id=req.study_id, field=field, old_value=old_val, new_value=val, reviewer=cfg.reviewer)

    return {"status": "success", "message": f"Updated field '{field}' for study '{req.study_id}'"}


@router.post("/projects/{project_id}/consolidation/figures")
def consolidation_generate_figures(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)

    phases = []
    for ph in range(1, cfg.n_phases + 1):
        ph_dir = state.phase_dir(ph)
        cand_p = ph_dir / "candidates.csv"
        dedup_p = ph_dir / "candidates_dedup.csv"
        sc_p = ph_dir / "screening.csv"

        c_df = pd.read_csv(cand_p) if cand_p.exists() else pd.DataFrame()
        d_df = pd.read_csv(dedup_p) if dedup_p.exists() else pd.DataFrame()
        s_df = pd.read_csv(sc_p) if sc_p.exists() else pd.DataFrame()

        phases.append(PhaseFrames(candidates=c_df, dedup=d_df, screening=s_df))

    inc_final_p = pdir / "included_final.csv"
    inc_df = pd.read_csv(inc_final_p) if inc_final_p.exists() else pd.DataFrame()

    counts = derive_prisma_counts_for_run(phases, inc_df)
    warnings = prisma_residuals(counts)

    outdir = pdir / "figures"
    extraction_path = pdir / "extraction.csv"

    cmd = [
        sys.executable, str(_SCRIPT_DIR / "figures.py"),
        "--run-dir", str(pdir),
        "--quality", str(extraction_path),
        "--outdir", str(outdir),
        "--identified", str(counts["identified"]),
        "--duplicates-removed", str(counts["duplicates_removed"]),
        "--screened", str(counts["screened"]),
        "--excluded-ta", str(counts["excluded_ta"]),
        "--assessed-ft", str(counts["assessed_ft"]),
        "--excluded-ft", str(counts["excluded_ft"]),
        "--included", str(counts["included"]),
    ]

    _run_script_subprocess(cmd)

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("figures_generated", counts=counts, warnings=warnings)

    generated_files = [f.name for f in outdir.glob("*")] if outdir.exists() else []

    return {
        "status": "success",
        "prisma_counts": counts,
        "warnings": warnings,
        "generated_files": generated_files,
    }


@router.post("/projects/{project_id}/consolidation/export-exclusions")
def consolidation_export_exclusions(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    included_path = pdir / "included_final.csv"

    if not included_path.exists():
        consolidation_merge_included(user_id, project_id)

    df = pd.read_csv(included_path)
    if "ft_decision" not in df.columns:
        raise HTTPException(status_code=400, detail="No full-text decisions recorded yet.")

    excluded = df[df["ft_decision"].astype(str).str.strip().str.lower().eq("exclude")]
    if excluded.empty:
        return {"status": "warning", "message": "No full-text exclusions recorded.", "n_excluded": 0}

    csv_path = pdir / "excluded_full_text.csv"
    bib_path = pdir / "excluded_full_text.bib"

    excluded.to_csv(csv_path, index=False, encoding="utf-8")
    bib_text = to_bibtex([row.to_dict() for _, row in excluded.iterrows()])
    bib_path.write_text(bib_text, encoding="utf-8")

    if "ft_reason" in excluded.columns:
        missing_reason = int((excluded["ft_reason"].isna() | (excluded["ft_reason"].astype(str).str.strip() == "")).sum())
    else:
        missing_reason = len(excluded)

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("export_ft_exclusions", n_excluded=len(excluded), n_missing_reason=missing_reason)

    return {
        "status": "success",
        "n_excluded": len(excluded),
        "n_missing_reason": missing_reason,
        "csv_filename": csv_path.name,
        "bib_filename": bib_path.name,
    }


@router.post("/projects/{project_id}/consolidation/kappa")
def consolidation_kappa(user_id: str, project_id: str, req: KappaReq):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    ph_dir = state.phase_dir(req.phase)

    if req.sheet_a_text:
        df_a = pd.read_csv(io.StringIO(req.sheet_a_text))
    else:
        path_a = ph_dir / "screening.csv"
        df_a = pd.read_csv(path_a) if path_a.exists() else pd.DataFrame()

    if req.sheet_b_text:
        df_b = pd.read_csv(io.StringIO(req.sheet_b_text))
    else:
        df_b = df_a.copy()

    stage = req.stage_col
    # Fallback resolution for stage decision column (e.g. 'decision', 'ta_decision', 'ft_decision')
    col_a = stage if stage in df_a.columns else next((c for c in ["decision", "ta_decision", "ft_decision", "status"] if c in df_a.columns), None)
    col_b = stage if stage in df_b.columns else next((c for c in ["decision", "ta_decision", "ft_decision", "status"] if c in df_b.columns), None)

    if df_a.empty or df_b.empty or not col_a or not col_b:
        raise HTTPException(status_code=400, detail=f"Both screening sheets must contain decision column '{stage}' (or standard fallback)")

    # Standardize column name for comparison
    if col_a != stage:
        df_a[stage] = df_a[col_a]
    if col_b != stage:
        df_b[stage] = df_b[col_b]

    # Handle id column fallback if missing (fallback to string index)
    if "id" not in df_a.columns:
        df_a["id"] = [str(i) for i in range(len(df_a))]
    if "id" not in df_b.columns:
        df_b["id"] = [str(i) for i in range(len(df_b))]

    rows_a = {str(row.get("id", "")): row.to_dict() for _, row in df_a.iterrows()}
    rows_b = {str(row.get("id", "")): row.to_dict() for _, row in df_b.iterrows()}

    res = compare_reviewers(rows_a, rows_b, stage_col=stage)

    if res.conflicts:
        out_conflicts = pdir / f"conflicts_{stage}.csv"
        pd.DataFrame(res.conflicts).to_csv(out_conflicts, index=False, encoding="utf-8")

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("inter_rater_agreement", stage=stage, n_compared=res.n_compared, n_agreed=res.n_agreed, kappa=res.kappa, percent_agreement=res.percent_agreement)

    return {
        "status": "success",
        "summary": res.summary(),
        "kappa": res.kappa,
        "percent_agreement": res.percent_agreement,
        "n_compared": res.n_compared,
        "n_agreed": res.n_agreed,
        "conflicts": res.conflicts,
        "matrix": res.matrix,
    }


@router.post("/projects/{project_id}/consolidation/methods-report")
def consolidation_methods_report(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)

    phases = _load_search_strategy_rows(state, cfg)
    counts = derive_prisma_counts_for_run([
        PhaseFrames(
            candidates=pd.read_csv(state.phase_dir(ph) / "candidates.csv") if (state.phase_dir(ph) / "candidates.csv").exists() else pd.DataFrame(),
            dedup=pd.read_csv(state.phase_dir(ph) / "candidates_dedup.csv") if (state.phase_dir(ph) / "candidates_dedup.csv").exists() else pd.DataFrame(),
            screening=pd.read_csv(state.phase_dir(ph) / "screening.csv") if (state.phase_dir(ph) / "screening.csv").exists() else pd.DataFrame(),
        )
        for ph in range(1, cfg.n_phases + 1)
    ], pd.read_csv(pdir / "included_final.csv") if (pdir / "included_final.csv").exists() else pd.DataFrame())

    paragraph = render_search_methods(cfg.to_dict(), phases, counts)
    strategy_table = render_search_strategy_table(phases)

    out_path = pdir / "search_methods.md"
    out_path.write_text(
        "# Search methods\n\n" + paragraph + "\n\n## Table S1\n\n" + strategy_table + "\n",
        encoding="utf-8"
    )

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("methods_report_drafted", n_phases=len(phases))

    return {
        "status": "success",
        "paragraph": paragraph,
        "strategy_table": strategy_table,
        "filename": out_path.name,
    }


@router.post("/projects/{project_id}/consolidation/review-self-appraisal")
def consolidation_review_self_appraisal(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)

    key = cfg.review_level_instrument or "AMSTAR_2"
    checklist = render_review_self_appraisal(key)
    out_path = pdir / f"review_self_appraisal_{key}.md"
    out_path.write_text(checklist, encoding="utf-8")

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("review_self_appraisal_drafted", instrument=key)

    return {
        "status": "success",
        "checklist_markdown": checklist,
        "filename": out_path.name,
    }


@router.post("/projects/{project_id}/consolidation/provenance")
def consolidation_provenance_report(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)

    phases = [
        PhaseFrames(
            candidates=pd.read_csv(state.phase_dir(ph) / "candidates.csv") if (state.phase_dir(ph) / "candidates.csv").exists() else pd.DataFrame(),
            dedup=pd.read_csv(state.phase_dir(ph) / "candidates_dedup.csv") if (state.phase_dir(ph) / "candidates_dedup.csv").exists() else pd.DataFrame(),
            screening=pd.read_csv(state.phase_dir(ph) / "screening.csv") if (state.phase_dir(ph) / "screening.csv").exists() else pd.DataFrame(),
        )
        for ph in range(1, cfg.n_phases + 1)
    ]
    inc_df = pd.read_csv(pdir / "included_final.csv") if (pdir / "included_final.csv").exists() else pd.DataFrame()
    counts = derive_prisma_counts_for_run(phases, inc_df)

    prov = Provenance(pdir / "provenance.jsonl")
    out_path = pdir / "PROVENANCE.md"
    prov.render_markdown(out_path, config=cfg.to_dict(), prisma=_prisma_report_rows(counts))

    markdown_text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""

    return {
        "status": "success",
        "markdown": markdown_text,
        "filename": out_path.name,
    }


@router.get("/projects/{project_id}/consolidation/diagnose")
def consolidation_diagnose_run(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)

    phase_nums = sorted({
        int(key.split(":", 1)[0]) for key in state.state.get("stages", {})
    }) or [1]

    funnel = []
    file_mismatches = []
    for phase in phase_nums:
        ph_dir = state.phase_dir(phase)
        stages_info = []
        for stage_key, filename, count_key, label in _PHASE_FUNNEL_STAGES:
            status = state.stage_status(phase, stage_key)
            entry = state.state.get("stages", {}).get(f"{phase}:{stage_key}", {})
            count = entry.get("counts", {}).get(count_key)

            file_exists = False
            if filename:
                fp = ph_dir / filename
                file_exists = fp.exists()
                if status == "done" and not file_exists:
                    file_mismatches.append({"phase": phase, "label": label, "filename": filename})

            stages_info.append({
                "stage": stage_key,
                "label": label,
                "status": status or "not_run",
                "count": count,
                "filename": filename,
                "file_exists": file_exists,
            })
        funnel.append({"phase": phase, "stages": stages_info})

    return {
        "status": "success",
        "phases_funnel": funnel,
        "file_mismatches": file_mismatches,
    }


@router.post("/projects/{project_id}/consolidation/rerun-search")
def consolidation_rerun_search(user_id: str, project_id: str, req: RerunSearchReq):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)

    changed = {}
    if req.mailto and req.mailto != cfg.mailto:
        cfg.mailto = req.mailto
        changed["mailto"] = req.mailto
    if req.year_from is not None:
        cfg.year_from = req.year_from
        changed["year_from"] = req.year_from
    if req.year_to is not None:
        cfg.year_to = req.year_to
        changed["year_to"] = req.year_to
    if req.max_per_source is not None:
        cfg.max_per_source = req.max_per_source
        changed["max_per_source"] = req.max_per_source
    if req.sources is not None:
        cfg.sources = req.sources
        changed["sources"] = req.sources

    if changed:
        state.save_config(cfg.to_dict())

    # Clear phase stages
    for stage in ("search", "dedup", "prescreen", "assist_ta", "review_gate"):
        state.state.get("stages", {}).pop(f"{req.phase}:{stage}", None)
    state.save()

    res_search = stage_search_harvest(user_id, project_id, phase=req.phase)
    res_dedup = stage_deduplicate(user_id, project_id, phase=req.phase)

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("rerun_search", phase=req.phase, changed_fields=list(changed.keys()), confirmed=True)

    return {
        "status": "success",
        "changed_settings": changed,
        "search_result": res_search,
        "dedup_result": res_dedup,
    }


@router.get("/version-check")
def api_version_check():
    latest = latest_release_version()
    outcome = "up_to_date"
    if VERSION.startswith("0.0.0-dev"):
        outcome = "dev_build"
    elif latest is None:
        outcome = "check_failed"
    elif is_newer(latest, VERSION):
        outcome = "outdated"

    return {
        "status": "success",
        "current_version": VERSION,
        "latest_version": latest,
        "outcome": outcome,
    }


# --- Pipeline Multi-Stage Stepper Endpoints ---

@router.post("/projects/{project_id}/stages/search")
def stage_search_harvest(user_id: str, project_id: str, phase: int = 1):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)
    ph_dir = state.phase_dir(phase)

    query_key = f"phase_{phase}_query"
    query = state.state.get(query_key) or cfg.search_query()
    state.state[query_key] = query
    state.save()

    candidates_path = ph_dir / "candidates.csv"
    strategy_path = ph_dir / "search_strategy.csv"

    cmd = [
        sys.executable, str(_SCRIPT_DIR / "search.py"),
        "--query", query,
        "--year-from", str(cfg.year_from),
        "--year-to", str(cfg.year_to),
        "--mailto", cfg.mailto,
        "--max-per-source", str(cfg.max_per_source),
        "--sources", ",".join(cfg.sources),
        "--out", str(candidates_path),
        "--strategy-log", str(strategy_path),
    ]
    if cfg.scopus_insttoken:
        cmd += ["--scopus-insttoken", cfg.scopus_insttoken]

    secret_env = _build_secret_env_for_user(user_id)
    code, stdout, stderr = _run_script_subprocess(cmd, secret_env=secret_env)

    if not candidates_path.exists():
        # Fallback empty dataframe if search script returned nothing or failed
        df = pd.DataFrame()
        df["id"] = []
        df["source"] = []
        df["title"] = []
        df["authors"] = []
        df["year"] = []
        df["venue"] = []
        df["doi"] = []
        df["url"] = []
        df["abstract"] = []
        df.to_csv(candidates_path, index=False, encoding="utf-8")
    else:
        df = pd.read_csv(candidates_path)

    n_hits = len(df)
    state.mark_stage(phase, "search", counts={"n_hits": n_hits})
    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("search_run", phase=phase, query=query, sources=cfg.sources, n_hits=n_hits)

    return {
        "status": "success" if code == 0 else "warning",
        "candidates_count": n_hits,
        "candidates": _clean_df_records(df),
        "stdout": stdout,
        "stderr": stderr,
    }


@router.post("/projects/{project_id}/stages/dedup")
def stage_deduplicate(user_id: str, project_id: str, phase: int = 1):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)
    ph_dir = state.phase_dir(phase)

    cand_path = ph_dir / "candidates.csv"
    dedup_path = ph_dir / "candidates_dedup.csv"
    screening_path = ph_dir / "screening.csv"

    if not cand_path.exists():
        stage_search_harvest(user_id, project_id, phase)

    cmd_dedup = [
        sys.executable, str(_SCRIPT_DIR / "dedup.py"),
        "--in", str(cand_path),
        "--out", str(dedup_path),
        "--title-threshold", str(cfg.title_threshold),
    ]
    _run_script_subprocess(cmd_dedup)

    cmd_screen = [
        sys.executable, str(_SCRIPT_DIR / "screen.py"),
        "--in", str(dedup_path),
        "--out", str(screening_path),
    ]
    _run_script_subprocess(cmd_screen)

    cand_df = pd.read_csv(cand_path) if cand_path.exists() else pd.DataFrame()
    dedup_df = pd.read_csv(dedup_path) if dedup_path.exists() else pd.DataFrame()
    screening_df = pd.read_csv(screening_path) if screening_path.exists() else pd.DataFrame()

    n_in = len(cand_df)
    n_dupes = int(dedup_df["duplicate_of"].notna().sum()) if "duplicate_of" in dedup_df.columns else 0
    n_out = len(dedup_df) - n_dupes

    state.mark_stage(phase, "dedup", counts={"n_in": n_in, "n_out": n_out, "n_dupes": n_dupes})
    state.mark_stage(phase, "prescreen", counts={"n_rows": len(screening_df)})

    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("dedup_run", phase=phase, n_in=n_in, n_out=n_out, n_dupes=n_dupes)

    unique_df = dedup_df[dedup_df["duplicate_of"].isna()] if "duplicate_of" in dedup_df.columns else dedup_df

    return {
        "status": "success",
        "duplicates_removed": n_dupes,
        "unique_candidates": len(unique_df),
        "candidates": _clean_df_records(unique_df),
    }


@router.get("/projects/{project_id}/stages/fulltext")
def get_fulltext_studies(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    inc_final_path = pdir / "included_final.csv"

    if not inc_final_path.exists():
        # Gather title/abstract includes from phase screening files
        state = RunState.load(pdir)
        records = []
        for ph in range(1, state.config.get("n_phases", 1) + 1):
            sc_path = state.phase_dir(ph) / "screening.csv"
            if sc_path.exists():
                sc_df = pd.read_csv(sc_path)
                if "ta_decision" in sc_df.columns:
                    inc_mask = ta_proceeds_mask(sc_df["ta_decision"])
                    inc_df = sc_df.loc[inc_mask].to_dict("records")
                    records.extend(inc_df)

        inc_df = pd.DataFrame(records) if records else pd.DataFrame()
        if not inc_df.empty:
            for col in ("id", "title", "authors", "year", "venue", "doi", "abstract"):
                if col not in inc_df.columns:
                    inc_df[col] = ""
        if "ft_decision" not in inc_df.columns:
            inc_df["ft_decision"] = ""
            inc_df["ft_reason"] = ""
        inc_df.to_csv(inc_final_path, index=False, encoding="utf-8")

    df = pd.read_csv(inc_final_path)
    return {"status": "success", "studies": _clean_df_records(df), "count": len(df)}


@router.post("/projects/{project_id}/stages/fulltext")
def record_fulltext_decision(user_id: str, project_id: str, req: FullTextDecisionReq):
    pdir = _get_user_run_dir(user_id, project_id)
    inc_final_path = pdir / "included_final.csv"

    if not inc_final_path.exists():
        get_fulltext_studies(user_id, project_id)

    df = pd.read_csv(inc_final_path)
    idx_match = df[df["id"].astype(str) == str(req.study_id)].index
    if len(idx_match) == 0:
        raise HTTPException(status_code=404, detail="Study not found in full-text screening list")

    df.at[idx_match[0], "ft_decision"] = req.decision.lower().strip()
    df.at[idx_match[0], "ft_reason"] = req.reason.strip()
    df.to_csv(inc_final_path, index=False, encoding="utf-8")

    state = RunState.load(pdir)
    state.mark_stage(req.phase, "full_text", counts={"decided_id": req.study_id, "decision": req.decision})

    return {"status": "success", "message": "Full-text decision recorded"}


@router.get("/projects/{project_id}/stages/appraisal")
def get_appraisal_instruments(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = state.config
    field = cfg.get("research_field", "software_engineering")
    instruments = cfg.get("primary_study_instruments", ["ACM_BADGE_V11"])

    inc_final_path = pdir / "included_final.csv"
    included_studies = []
    if inc_final_path.exists():
        df = pd.read_csv(inc_final_path)
        if "ft_decision" in df.columns:
            inc_mask = df["ft_decision"].astype(str).str.strip().str.lower().isin(["include", "yes", "inc"])
            included_studies = df[inc_mask].to_dict("records")
        else:
            included_studies = df.to_dict("records")

    instrument_fields = {}
    for inst in instruments:
        instrument_fields[inst] = instrument_columns(inst)

    return {
        "status": "success",
        "research_field": field,
        "instruments": instruments,
        "instrument_columns": instrument_fields,
        "included_studies": _clean_df_records(pd.DataFrame(included_studies)) if included_studies else [],
    }


@router.get("/projects/{project_id}/stages/prisma")
def get_prisma_diagram_data(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)

    phases = []
    for ph in range(1, state.config.get("n_phases", 1) + 1):
        ph_dir = state.phase_dir(ph)
        cand_p = ph_dir / "candidates.csv"
        dedup_p = ph_dir / "candidates_dedup.csv"
        sc_p = ph_dir / "screening.csv"

        c_df = pd.read_csv(cand_p) if cand_p.exists() else pd.DataFrame()
        d_df = pd.read_csv(dedup_p) if dedup_p.exists() else pd.DataFrame()
        s_df = pd.read_csv(sc_p) if sc_p.exists() else pd.DataFrame()

        phases.append(PhaseFrames(candidates=c_df, dedup=d_df, screening=s_df))

    inc_final_p = pdir / "included_final.csv"
    inc_df = pd.read_csv(inc_final_p) if inc_final_p.exists() else pd.DataFrame()

    counts = derive_prisma_counts_for_run(phases, inc_df)
    warnings = prisma_residuals(counts)

    return {
        "status": "success",
        "prisma_counts": counts,
        "warnings": warnings,
    }


@router.get("/projects/{project_id}/export/{export_format}")
def export_references(user_id: str, project_id: str, export_format: str):
    pdir = _get_user_run_dir(user_id, project_id)
    inc_path = pdir / "included_final.csv"
    ext_path = pdir / "extraction.csv"

    target_path = ext_path if ext_path.exists() else inc_path
    if not target_path.exists():
        raise HTTPException(status_code=400, detail="No exportable studies found.")

    df = pd.read_csv(target_path)
    records = _clean_df_records(df)

    if export_format.lower() == "bibtex":
        return {"status": "success", "content": to_bibtex(records), "filename": "references.bib"}
    elif export_format.lower() == "ris":
        return {"status": "success", "content": to_ris(records), "filename": "references.ris"}
    else:
        raise HTTPException(status_code=400, detail="Unsupported format. Use 'bibtex' or 'ris'.")
