from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from srp.appraisal import FIELD_PROFILES, INSTRUMENTS, PRIMARY_STUDY, compose_appraisal_disclosure
from srp.config import ReviewConfig
from srp.decisions import apply_decisions, ta_proceeds_mask
from srp.export import to_bibtex, to_ris
from srp.llm_assist import build_screening_prompt, compose_criteria, parse_screening_response
from srp.methods_report import PhaseSearchRecord, SourceStrategyRow, render_search_methods, render_search_strategy_table
from srp.prisma import PhaseFrames, derive_prisma_counts_for_run, prisma_residuals
from srp.provenance import Provenance
from srp.quality_tier import compute_quality_tier
from srp.state import RunState, record_key

router = APIRouter()

BASE_RUNS_DIR = Path("runs_web")
BASE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
MAX_PROJECTS_PER_USER = 5


# --- Schemas ---
class UserAuth(BaseModel):
    user_id: str
    email: Optional[str] = None


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


class FullTextDecisionReq(BaseModel):
    candidate_id: str
    decision: str  # include | exclude
    reason: str = ""


class ExtractionUpdateReq(BaseModel):
    study_id: str
    field: str
    value: str


# --- Helpers ---
def _get_user_run_dir(user_id: str, project_id: str) -> Path:
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
    return len([d for d in user_dir.iterdir() if d.is_dir()])


# --- Endpoints ---

@router.get("/projects")
def list_projects(user_id: str):
    user_dir = BASE_RUNS_DIR / user_id
    if not user_dir.exists():
        return {"projects": [], "count": 0, "max_allowed": MAX_PROJECTS_PER_USER}

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
    return {"projects": projects, "count": len(projects), "max_allowed": MAX_PROJECTS_PER_USER}


@router.post("/projects")
def create_project(req: ProjectCreateReq):
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

    return {"project_id": project_id, "message": "Project created successfully"}


@router.delete("/projects/{project_id}")
def delete_project(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    import shutil
    shutil.rmtree(pdir)
    return {"message": f"Project {project_id} deleted successfully"}


@router.get("/projects/{project_id}/status")
def get_project_status(user_id: str, project_id: str):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    return {
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

    import pandas as pd
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
        return {"prompt": None, "message": "No undecided candidates remaining."}

    criteria = compose_criteria("ta", cfg.inclusion_criteria, cfg.exclusion_criteria)
    records = [
        {"id": r.get("id"), "title": r.get("title", ""), "abstract": r.get("abstract", ""),
         "year": r.get("year", ""), "venue": r.get("venue", "")}
        for r in batch
    ]
    prompt = build_screening_prompt(records, stage="ta", topic=cfg.topic, criteria=criteria)
    return {
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
        "applied_counts": applied.counts,
        "matched_ids": applied.matched,
        "problems": applied.problems(),
    }


@router.post("/projects/{project_id}/review-gate")
def apply_review_gate(user_id: str, project_id: str, req: ReviewGateReq):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)
    ph_dir = state.phase_dir(req.phase)
    screening_path = ph_dir / "screening.csv"

    import pandas as pd
    if not screening_path.exists():
        raise HTTPException(status_code=400, detail="Screening file not found.")

    df = pd.read_csv(screening_path)
    for idx, row in df.iterrows():
        rid = str(row.get("id"))
        if rid in req.to_exclude:
            df.at[idx, "ta_decision"] = "exclude"
            df.at[idx, "ta_reason"] = f"{df.at[idx, 'ta_reason'] or ''} [reviewer override]".strip()
            df.at[idx, "reviewer"] = cfg.reviewer
        elif rid in req.to_include:
            df.at[idx, "ta_decision"] = "include"
            df.at[idx, "ta_reason"] = f"{df.at[idx, 'ta_reason'] or ''} [reviewer override]".strip()
            df.at[idx, "reviewer"] = cfg.reviewer

    df.to_csv(screening_path, index=False, encoding="utf-8")
    state.mark_stage(req.phase, "review_gate", counts={"n_overridden": len(req.to_exclude) + len(req.to_include)})

    return {"message": "Review gate updated successfully"}


@router.get("/projects/{project_id}/export/{export_format}")
def export_references(user_id: str, project_id: str, export_format: str):
    pdir = _get_user_run_dir(user_id, project_id)
    import pandas as pd
    inc_path = pdir / "included_final.csv"
    ext_path = pdir / "extraction.csv"

    target_path = ext_path if ext_path.exists() else inc_path
    if not target_path.exists():
        raise HTTPException(status_code=400, detail="No exportable studies found.")

    df = pd.read_csv(target_path)
    records = df.to_dict("records")

    if export_format.lower() == "bibtex":
        return {"content": to_bibtex(records), "filename": "references.bib"}
    elif export_format.lower() == "ris":
        return {"content": to_ris(records), "filename": "references.ris"}
    else:
        raise HTTPException(status_code=400, detail="Unsupported format. Use 'bibtex' or 'ris'.")
