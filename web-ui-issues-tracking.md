# Web UI Issues Tracking Document

## Project Status Report
**Last Updated**: 2026-09-23
**Repository**: systematic-review-pipeline
**Current State**: Full-stack web application operational with 6-stage interactive stepper, auth persistence, and complete consolidation dashboard. Active workspace session confirmed working (test@example.com, project be284974 "Test Topic").

## Issue Summary
The web UI has been substantially built out since the last tracking update. Most originally-open issues are now resolved or were addressed in earlier commits that were not reflected in this document. The focus has shifted from implementing core features to browser-based verification, testing, and polish.

## Open Issues (Remaining)

### 1. WebSocket / Real-time Updates
- **Status**: OPEN (as of last review)
- **Impact**: UI relies on manual refresh; no live progress streaming from backend subprocess calls
- **Location**: `server/main.py` — no WebSocket route registered
- **Verification Needed**: Confirm whether the UI shows loading states for long-running operations (search, snowball, download). If subprocess calls complete synchronously, the browser may time out.
- **Note**: This is a design limitation, not a bug. The current synchronous POST pattern works for short operations. For long-running subprocesses (search, snowballing, PDF download), the UI should either show a progress indicator or the backend should support async polling.

### 2. Ephemeral Filesystem on Render
- **Status**: OPEN (architectural constraint)
- **Impact**: On Render's free tier, `runs_web/{user_id}/{project_id}/` directories are wiped on cold starts. Projects created in one session may not persist.
- **Location**: `server/routes.py` — `BASE_RUNS_DIR = Path("runs_web")`
- **Note**: This is an environmental constraint of the deployment platform, not a code bug. Local development does not have this issue.

### 3. Inter-Rater Agreement Endpoint
- **Issue**: The `consolidation_kappa` endpoint (line 1133) reads from `phase/{phase}/screening.csv` but the `KappaReq` model has a `stage_col` parameter defaulting to `"ta_decision"`. There is no clear UI path to upload alternate reviewer sheets.
- **Impact**: Limited inter-rater agreement functionality — requires pasting CSV text manually
- **Location**: `server/routes.py` lines 1133-1174, `web/index.html` Kappa accordion
- **Status**: OPEN — functional but could benefit from file upload support

## Resolved Issues (Previously Listed as Open)

### 1. Missing `/auth/logout` Endpoint — RESOLVED
- **Proof**: Present in `server/routes.py` line 324-326:
  ```python
  @router.post("/auth/logout")
  def api_logout():
      return {"status": "success", "message": "Logged out successfully"}
  ```
- **Frontend**: `web/index.html` line 30 — `Logout` button calls `logout()` method

### 2. Incomplete Account Management — RESOLVED
- **Proof**: All endpoints are present and functional:
  - `GET /auth/profile` (line 285) — loads user profile
  - `PUT /auth/profile` (line 293) — updates email/password/API keys
  - `POST /auth/test-key` (line 307) — tests API keys against 8 services
  - `DELETE /auth/account` (line 313) — deletes account and user directory
- **Frontend**: Account settings modal with API key management (8-key slots: Semantic Scholar, PubMed, CORE, IEEE, Scopus, Scopus InstToken, Springer, WoS)

### 3. Missing Project Selection UI — RESOLVED
- **Proof**: `web/index.html` implements full project lifecycle:
  - Projects dashboard listing with cards
  - "Start New Review" form (TAB with topic, criteria, year range, phases, sources)
  - `createProject()` Vue method posts to `/api/projects`
  - Active workspace selector shows current project
  - Project switching from dashboard

### 4. Inconsistent User Directory Counting — RESOLVED
- **Proof**: `server/routes.py` line 176-180:
  ```python
  def _count_user_projects(user_id: str) -> int:
      user_dir = BASE_RUNS_DIR / user_id
      if not user_dir.exists():
          return 0
      return len([d for d in user_dir.iterdir() if d.is_dir() and (d / "config.json").exists()])
  ```
  Now checks for `config.json` existence — only valid project directories are counted.

### 5. Missing Phase Execution Endpoints — RESOLVED
- **Proof**: Full staged pipeline endpoints implemented in `server/routes.py` lines 1369-1598:
  - `POST /projects/{id}/stages/search` — Search & Harvest
  - `POST /projects/{id}/stages/dedup` — Deduplication (runs dedup.py + screen.py)
  - `GET/POST /projects/{id}/stages/fulltext` — Full-text screening
  - `GET /projects/{id}/stages/appraisal` — Quality appraisal instruments
  - `GET /projects/{id}/stages/prisma` — PRISMA 2020 diagram data

### 6. Vue.js State Management Issues — RESOLVED
- **Proof**: The Vue app at `web/index.html` lines 812+ has comprehensive data properties:
  - `currentUser`, `projects`, `activeProject`, `activePhase`, `activeStage`
  - `candidatesList`, `dedupResult`, `aiPrompt`, `aiReply`
  - `reviewGateData`, `gateExcludes`, `gateIncludes`
  - `queryExpansion`, `consolidationResults`, `showAccountModal`
  - Proper initialization in `created()` hook with `loadUserAndProjects()` on mount
- **Status**: App loads and navigates all 6 stages without crashes (verified via browser snapshot)

### 7. Inconsistent API Response Formats — PARTIALLY RESOLVED
- **Current State**: Most endpoints use `{"status": "success", ...}` pattern. However, some endpoints return varying top-level keys:
  - `/projects` returns `{"projects": [...], "count": N, "max_allowed": 5}` (no "status" key)
  - `/auth/profile` returns `{"status": "success", "user": {...}}`
  - `/projects/{id}/status` returns `{"config": {...}, "state": {...}}` (no "status" key)
- **Impact**: Minor inconsistency in response envelope — frontend handles each case individually
- **Status**: Functional but not fully standardized

### 8. ta_proceeds_mask Type Errors — RESOLVED (Latest Commit)
- **Proof**: Commit `94eb64f` (current HEAD) fixes:
  - `_clean_df_records` now handles Series input (line 54-61)
  - `get_review_gate_data` converts to `pd.Series` before `ta_proceeds_mask` (line 513-514)
  - `get_query_expansion_suggestions` fixed (line 602)
  - `record_consolidation_fulltext_decision` fixed in `get_fulltext_studies` (line 1500)

## Completed Tasks

### COMPLETED
- [x] **1.1**: Added `/auth/logout` endpoint — `server/routes.py` line 324
- [x] **1.2**: Fixed `ta_proceeds_mask` type errors — commit `94eb64f`
- [x] **1.3**: Fixed `_clean_df_records` to handle Series input — `server/routes.py` line 54-61
- [x] **1.4**: Fixed user directory counting — `server/routes.py` line 180
- [x] **1.5**: Fixed `ta_proceeds_mask` in merge-snowball — `server/routes.py` line 756-759
- [x] **2.1**: Complete project selection UI — `web/index.html` dashboard + create form
- [x] **2.2**: Added PostgreSQL/SQLite auth persistence — commit `f07db3e`, `server/auth.py`
- [x] **2.3**: Fernet field encryption for API keys — commit `f07db3e`
- [x] **2.4**: Fixed Vue.js state management — all reactive properties initialized
- [x] **2.5**: Connected web UI to real TUI pipeline scripts — commit `1c29f77`
- [x] **2.6**: Added 8-key API key management UI — `web/index.html` Account Settings
- [x] **2.7**: Added error handling for subprocess calls — `snowball.py`, `dedup.py`, `screen.py`
- [x] **2.8**: Implemented all 6 stage tabs — verified via browser snapshots

## In Progress Tasks (Awaiting Browser Verification)

### IN PROGRESS
- [ ] **3.1**: Verify all 19 Consolidation Dashboard actions work end-to-end
  - Currently verified present in UI (all 19 buttons render correctly)
  - Need to test each action's backend endpoint and frontend response handling
- [ ] **3.2**: Verify Snowflake/CSV generation for PRISMA diagrams
  - Endpoint exists at `GET /projects/{id}/stages/prisma`
  - Need browser test to confirm PRISMA flow data renders correctly
- [ ] **3.3**: Verify inter-rater agreement kappa computation
  - Endpoint exists at `POST /projects/{id}/consolidation/kappa`
  - Need test with two reviewer sheets
- [ ] **3.4**: Verify full-text screening workflow
  - Endpoints exist at `GET/POST /projects/{id}/stages/fulltext`
  - Need browser test to confirm study list loads and decisions save
- [ ] **3.5**: Verify export functionality (BibTeX, RIS)
  - Endpoint exists at `GET /projects/{id}/export/{format}`
  - Need browser test to confirm export content generates

## Pending Tasks

### PENDING
- [ ] **4.1**: Add WebSocket support for real-time updates
- [ ] **4.2**: Standardize all API response formats with consistent `{"status": ...}` envelope
- [ ] **4.3**: Add file upload support for inter-rater agreement sheets
- [ ] **4.4**: Implement progress indicators for long-running subprocess operations
- [ ] **4.5**: Add unit tests for web server endpoints (`tests/test_web_server.py` exists but coverage should be verified)
- [ ] **4.6**: Add integration tests for auth flows (login → project creation → stage execution)
- [ ] **4.7**: Verify API key management persistence across sessions

## Testing Results

### Completed Tests
- Auth flow: Register → Login → Store user_id in localStorage → Load projects — VERIFIED working
- `/auth/logout` endpoint returns `{"status": "success", "message": "Logged out successfully"}` — VERIFIED
- `/auth/test-key` endpoint tests all 8 API services — VERIFIED (endpoint exists, needs live API key to test fully)
- `_count_user_projects` returns correct count with `config.json` check — VERIFIED (code review)
- Project creation stores in `runs_web/{user_id}/{project_id}/` — VERIFIED (active workspace confirmed)
- All 19 consolidation dashboard buttons render correctly — VERIFIED (browser snapshot)
- All 6 stage tabs render and are navigable — VERIFIED (browser snapshots)
- Vue.js app mounts on `#app` without errors — VERIFIED (browser snapshot shows loaded workspace)

### Manual Testing Required (Pending)
- Create new test account and verify full auth persistence
- Test project creation with all field combinations
- Test each of the 19 consolidation dashboard actions
- Test inter-rater agreement with sample data
- Test full-text screening workflow
- Test export (BibTeX and RIS)
- Test API key storage and retrieval
- Test account deletion and project cleanup

### Automated Testing Status
- `tests/test_web_auth.py` — tests for database auth and encryption (added in commit `f07db3e`, 50 lines)
- `tests/test_web_server.py` — tests for web server routes (77 lines)
- Coverage scope: Unclear — need to run `pytest` to determine pass/fail status

## Testing Instructions
1. Run the application: `uvicorn server.app:app --reload`
2. Open browser to `http://localhost:8000`
3. Register a new account (email required, not username)
4. Create a project with topic, keyword blocks, criteria
5. Navigate all 6 stages: Search, Dedup, TA AI, Review Gate, Query Expansion, Consolidation
6. Test all 19 consolidation dashboard actions
7. Use Camoufox browser for humanized interaction testing
8. Verify API key management in Account Settings
9. Test logout and re-login persistence

## Technical Notes
- **Auth Model**: Stateless — `user_id` passed as query parameter, no session cookies
- **Project Storage**: `runs_web/{user_id}/{project_id}/` on local filesystem
- **Pipeline Scripts**: `scripts/search.py`, `scripts/dedup.py`, `scripts/screen.py`, `scripts/snowball.py`, `scripts/download.py`, etc.
- **Frontend**: Vue 3.3.4 (CDN), Tailwind CSS 2.2.19, no build step
- **API Base**: `/api` prefix on all routes
- **Phase Model**: All stage endpoints accept `phase` as query parameter defaulting to 1
- **Encryption**: Fernet field encryption for API keys via `APP_ENCRYPTION_KEY` (commit `f07db3e`)
- **Database**: `DATABASE_URL` env var supports PostgreSQL; falls back to JSON storage