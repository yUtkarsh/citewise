# Code Graph Agent

Turns a digital building-code PDF into a FalkorDB graph, then a chat agent answers
design questions with a traced, clause-level path through that graph.
Built for Graph Hacks (WeMakeDevs x FalkorDB). Deadline: Oct 18, 2026, 11:59 PM IST.

**The full spec is in `SPEC.md`. Read the relevant step before starting any phase.**

## Core principle

Claude (via the API) makes judgments that generalize: rules, meanings, reading symbols
from images. Deterministic Python applies those judgments and verifies every result.
FalkorDB holds every fact the agent uses. The agent never does arithmetic itself.

## Stack

- Python 3.11+, FastAPI, Server-Sent Events
- FalkorDB in Docker (`docker compose up`); Python client `falkordb`
- PyMuPDF for text, fonts and crops; Docling only on table pages (PyMuPDF `find_tables()` fallback)
- Anthropic Python SDK; models come from `.env`, never hard-coded
- SymPy for equations; sentence-transformers for local embeddings
- Frontend: one static page with Cytoscape.js and KaTeX (no build step)

## Folder layout

```
app/
  profile/      # Step 2: page map, Claude profile, validation
  ingest/       # Step 3a (pass1/) and 3b (pass2/), 3c (global_pass.py)
  graph/        # FalkorDB connection, schema, Cypher queries, writers
  agent/        # Step 5: tools, tool loop, compute
  memory/       # Step 6: memory graph, session facts
  api/          # FastAPI routes, SSE
  prompts/      # one file per Claude prompt, versioned
web/            # index.html, app.js, styles.css
tests/
fixtures/       # public FEMA 356 sample pages only
private_fixtures/  # licensed pages, git-ignored, never committed
data/           # uploads, crops, cache; git-ignored
```

## Machine

Native Windows, PowerShell, no WSL. You (Claude Code) handle installation: check what is
already installed before installing anything, prefer `winget` for system tools and `pip`
inside the `.venv` for Python packages, and ask before every system-level install. Python in a `.venv` (activate with
`.\.venv\Scripts\Activate.ps1`). Docker Desktop runs FalkorDB. Use Windows-safe paths
(`pathlib`), and PowerShell syntax for any commands you run or suggest.

## Commands

- Start FalkorDB: `docker compose up -d` (browser UI on http://localhost:3000)
- Run app: `uvicorn app.api.main:app --reload`
- Tests: `pytest -q`

## Environment (`.env`, never committed)

```
ANTHROPIC_API_KEY=...
MODEL_NOTATION=claude-sonnet-5-5
MODEL_TABLES=claude-sonnet-5-5
MODEL_CLAUSES=claude-haiku-5-5
MODEL_AGENT=claude-sonnet-5-5
MODEL_REVIEW=claude-sonnet-5-5
FALKORDB_HOST=localhost
FALKORDB_PORT=6379
```

Load with python-dotenv inside the app. Do not export ANTHROPIC_API_KEY in the shell.

## Rules

- Work one build-order phase at a time (see `SPEC.md`, Build order). Finish its checkpoint before the next.
- Every Claude API output is validated with Pydantic, then by the deterministic checks in SPEC.md. Never trust raw output.
- Every graph node carries: page, bbox (if any), source_quote, confidence, extraction_method, review_status, schema_version.
- Graph writes use `UNWIND ... MERGE` so re-runs never duplicate.
- Cache every Claude call by hash of (input text, schema version, prompt version) under `data/cache/`.
- `run_cypher` must use FalkorDB read-only queries.
- Never commit anything from `private_fixtures/` or `data/`.
- Write a test with a fixture page for each new extraction function.
- Keep functions small and typed; prefer clear code over clever code.
