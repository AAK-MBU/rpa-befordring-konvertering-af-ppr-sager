# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (uses uv)
uv sync

# Run linting
uv run ruff check .
uv run ruff format .

# Run the process phases (requires environment variables set)
python main.py --queue      # populate workqueue from source data
python main.py --process    # process items from workqueue
python main.py --finalize   # run finalization logic
```

## Architecture

This is a Danish municipal RPA (Robotic Process Automation) bot that converts/migrates PPR (Pædagogisk Psykologisk Rådgivning) cases into GetOrganized (GO), the municipality's case management system.

### Execution flow

`main.py` is the entry point with three independently invocable phases controlled by CLI flags:

1. **`--queue`** — `populate_queue()` fetches items from source systems via `RPAConnection` (RPA database), builds a list of work items, deduplicates against existing queue entries, then calls `concurrent_add()` with async concurrency + exponential-backoff retries.

2. **`--process`** — `process_workqueue()` iterates the ATS workqueue. Each item calls `process_item()`. Errors are bifurcated: `BusinessError` → `item.pending_user()` (no mail), `ProcessError` → `item.fail()` + email alert. After `MAX_RETRY` consecutive failures the loop aborts. Application is reset between failures.

3. **`--finalize`** — `finalize_process()` runs post-processing cleanup.

### Key modules

| Module | Purpose |
|---|---|
| `helpers/config.py` | Central constants (`MAX_RETRY`, concurrency/retry settings) |
| `helpers/ats_functions.py` | ATS workqueue inspection via REST API, logger init |
| `helpers/case_handler.py` | `CaseHandler` — wraps `mbu_dev_shared_components` to create/search cases and folders in GetOrganized |
| `helpers/document_handler.py` | `DocumentHandler` — uploads, journalizes, and finalizes documents in GetOrganized |
| `helpers/journalize_process.py` | High-level orchestration functions for the full journalization flow (contact lookup → case folder → case → document upload → journalize/finalize) |
| `helpers/helper_functions.py` | Utility functions |
| `processes/queue_handler.py` | `retrieve_items_for_queue()` builds item list; `concurrent_add()` populates ATS workqueue |
| `processes/process_item.py` | Single-item processing logic (stub — implement here) |
| `processes/application_handler.py` | App lifecycle: `startup()`, `close()`, `reset()` |
| `processes/error_handling.py` | `ErrorContext` + `handle_error()` — uniform error dispatch |
| `processes/finalize_process.py` | Post-processing finalization |

### GetOrganized (GO) integration layer

All GO API calls go through two thin wrapper classes defined in `helpers/`:

- **`CaseHandler`** (`helpers/case_handler.py`) — delegates to `mbu_dev_shared_components.getorganized`
- **`DocumentHandler`** (`helpers/document_handler.py`) — delegates to `mbu_dev_shared_components.getorganized`

Both use NTLM authentication (handled transparently by `mbu_dev_shared_components.getorganized.auth`).

The underlying `mbu_dev_shared_components.getorganized` modules (installed in `.venv`):

| Module | Key functions |
|---|---|
| `api` | `health_check()` — GET `/_api/web`, returns bool |
| `cases` | `get_case_metadata()`, `find_case_by_case_properties()`, `create_case_folder()`, `create_case()` |
| `contacts` | `contact_lookup(person_ssn)` — POST to `/_goapi/contacts/readitem`, returns `{FullName, ID}` |
| `documents` | `upload_file_to_case()`, `mark_file_as_case_record()`, `finalize_file()`, `search_documents()`, `modern_search()` |
| `objects` | `CaseDataJson` — builds metadata XML + JSON payloads; `DocumentJsonCreator` — builds document upload payloads; `CaseTypePrefix` literal (`"BOR"`, `"PPR"`, `"EMN"`, …) |

**Case metadata XML format**: fields are `ows_*` attributes on a `<z:row xmlns:z="#RowsetSchema" .../>` element. Special fields: `ows_CCMContactData` (`"FullName;#GoId;#SSN;#;#"`), `ows_CCMParentCase` (`"CaseFolderId;#Prefix"`), `ows_Sagsprofil_{Prefix}` (`"ProfileId;#ProfileName"`).

**Standard GO API endpoints used**:
- `/_goapi/Cases` — create case or case folder (POST)
- `/_goapi/cases/findbycaseproperties` — search cases (POST)
- `/_goapi/contacts/readitem` — contact lookup by SSN (POST)
- `/_goapi/Documents/AddToCase` — upload document (POST)
- `/_goapi/Documents/MarkMultipleAsCaseRecord/ByDocumentId` — journalize (POST)
- `/_goapi/Documents/FinalizeMultiple/ByDocumentId` — finalize (POST)
- `/_goapi/Search/Results` — keyword document search (POST)
- `/_api/web` — health check (GET)

### External systems

- **ATS (Automation Server)** — workqueue backend; credentials via env vars `ATS_URL` and `ATS_TOKEN`
- **GetOrganized (GO)** — SharePoint-based case management; credentials fetched from RPA database at runtime via `RPAConnection`
- **OS2Forms** — digital forms platform; API key fetched from RPA database
- **RPA database** — central credential/constant store accessed via `RPAConnection(db_env="PROD")`

### Error handling pattern

`BusinessError` (expected, user-actionable) vs `ProcessError` (system failure requiring robot restart and email notification). Both are from `mbu_rpa_core.exceptions`.

### Important note

`main.py` contains a temporary SSL verification bypass block marked with `🔥 REMOVE BEFORE DEPLOYMENT`. This disables certificate verification for all `requests` calls and must be removed before deploying to production.
