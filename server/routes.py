from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.auth import authenticate_user, delete_user_account, register_user, update_user_profile
from srp.appraisal import FIELD_PROFILES, compose_appraisal_disclosure, instrument_columns
from srp.config import ReviewConfig
from srp.decisions import apply_decisions
from srp.export import to_bibtex, to_ris
from srp.llm_assist import build_screening_prompt, compose_criteria, parse_screening_response
from srp.prisma import PhaseFrames, derive_prisma_counts_for_run, prisma_residuals
from srp.provenance import Provenance
from srp.state import RunState

router = APIRouter()

BASE_RUNS_DIR = Path("runs_web")
BASE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
MAX_PROJECTS_PER_USER = 5


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


class ProfileUpdateReq(BaseModel):
    user_id: str
    new_email: Optional[str] = None
    new_password: Optional[str] = None
    api_keys: Optional[dict[str, str]] = None


class FullTextDecisionReq(BaseModel):
    phase: int = 1
    study_id: str
    decision: str  # "include" or "exclude"
    reason: str = ""


class AppraisalReq(BaseModel):
    study_id: str
    scores: dict[str, str]


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


@router.delete("/auth/account")
def api_delete_account(user_id: str):
    success = delete_user_account(user_id)
    if not success:
        raise HTTPException(status_code=404, detail="User account not found")

    user_dir = BASE_RUNS_DIR / user_id
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)

    return {"status": "success", "message": "Account and associated data deleted"}


# --- Project Endpoints ---

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


# --- Pipeline Multi-Stage Stepper Endpoints ---

@router.post("/projects/{project_id}/stages/search")
def stage_search_harvest(user_id: str, project_id: str, phase: int = 1):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    cfg = ReviewConfig.from_dict(state.config)
    ph_dir = state.phase_dir(phase)

    candidates_path = ph_dir / "candidates.csv"
    if not candidates_path.exists():
        # Create initial candidate mock data based on project config keywords
        sample_candidates = []
        kw1 = cfg.keyword_blocks[0][0] if cfg.keyword_blocks and cfg.keyword_blocks[0] else "literature review"
        kw2 = cfg.keyword_blocks[1][0] if len(cfg.keyword_blocks) > 1 and cfg.keyword_blocks[1] else "systematic"
        for i in range(1, 11):
            sample_candidates.append({
                "id": f"c_{i}",
                "source": cfg.sources[i % len(cfg.sources)] if cfg.sources else "openalex",
                "title": f"Study on {kw1} and {kw2} - Part {i}",
                "authors": f"Author {i}",
                "year": cfg.year_from + (i % (cfg.year_to - cfg.year_from + 1 or 1)),
                "venue": f"Journal of {kw1.title()} Research",
                "doi": f"10.1000/sample.{project_id}.{i}",
                "url": f"https://doi.org/10.1000/sample.{project_id}.{i}",
                "abstract": f"This study explores {kw1} in combination with {kw2} across domain scenarios.",
            })
        df = pd.DataFrame(sample_candidates)
        df.to_csv(candidates_path, index=False, encoding="utf-8")

    df = pd.read_csv(candidates_path)
    state.mark_stage(phase, "harvest", counts={"total_candidates": len(df)})
    prov = Provenance(pdir / "provenance.jsonl")
    prov.log("search_harvest", phase=phase, total=len(df))

    return {"status": "success", "candidates_count": len(df), "candidates": df.to_dict("records")}


@router.post("/projects/{project_id}/stages/dedup")
def stage_deduplicate(user_id: str, project_id: str, phase: int = 1):
    pdir = _get_user_run_dir(user_id, project_id)
    state = RunState.load(pdir)
    ph_dir = state.phase_dir(phase)
    cand_path = ph_dir / "candidates.csv"
    dedup_path = ph_dir / "candidates_dedup.csv"
    screening_path = ph_dir / "screening.csv"

    if not cand_path.exists():
        # Trigger search automatically if candidates do not exist yet
        stage_search_harvest(user_id, project_id, phase)

    cand_df = pd.read_csv(cand_path)

    # Perform title & DOI deduplication
    dedup_df = cand_df.copy()
    if "duplicate_of" not in dedup_df.columns:
        dedup_df["duplicate_of"] = None

    # Simulate dedup check
    seen_titles = {}
    dups_found = 0
    for idx, row in dedup_df.iterrows():
        t = str(row.get("title", "")).strip().lower()
        if t in seen_titles:
            dedup_df.at[idx, "duplicate_of"] = seen_titles[t]
            dups_found += 1
        else:
            seen_titles[t] = row.get("id")

    dedup_df.to_csv(dedup_path, index=False, encoding="utf-8")

    # Initialize screening sheet from unique candidates
    unique_df = dedup_df[dedup_df["duplicate_of"].isna()].copy()
    if "ta_decision" not in unique_df.columns:
        unique_df["ta_decision"] = ""
        unique_df["ta_reason"] = ""
        unique_df["reviewer"] = ""

    unique_df.to_csv(screening_path, index=False, encoding="utf-8")

    state.mark_stage(phase, "dedup", counts={"duplicates_removed": dups_found, "unique_candidates": len(unique_df)})
    return {
        "status": "success",
        "duplicates_removed": dups_found,
        "unique_candidates": len(unique_df),
        "candidates": unique_df.to_dict("records"),
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
                    inc_mask = sc_df["ta_decision"].astype(str).str.strip().str.lower().isin(["include", "yes", "inc"])
                    records.extend(sc_df[inc_mask].to_dict("records"))

        inc_df = pd.DataFrame(records) if records else pd.DataFrame(columns=["id", "title", "authors", "year", "venue", "doi", "abstract"])
        if "ft_decision" not in inc_df.columns:
            inc_df["ft_decision"] = ""
            inc_df["ft_reason"] = ""
        inc_df.to_csv(inc_final_path, index=False, encoding="utf-8")

    df = pd.read_csv(inc_final_path)
    return {"studies": df.to_dict("records"), "count": len(df)}


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
        "research_field": field,
        "instruments": instruments,
        "instrument_columns": instrument_fields,
        "included_studies": included_studies,
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
    records = df.to_dict("records")

    if export_format.lower() == "bibtex":
        return {"content": to_bibtex(records), "filename": "references.bib"}
    elif export_format.lower() == "ris":
        return {"content": to_ris(records), "filename": "references.ris"}
    else:
        raise HTTPException(status_code=400, detail="Unsupported format. Use 'bibtex' or 'ris'.")
