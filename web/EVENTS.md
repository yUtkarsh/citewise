# Chat event contract (frontend ⇄ agent)

The chat page (`web/chat.html`) talks to the backend through the routes in
`app/api/chat_routes.py` (`router`). The library, upload, ingest and report pages use
`app/api/ingest_routes.py` (`router`, see the end of this file). Until `app/api/main.py`
includes both routers, run the combined app, which also serves `web/`:

```powershell
uvicorn app.api.web_app:app --reload      # then open http://127.0.0.1:8000/ (the library)
```

To merge it into the main app later (three lines in `main.py`):

```python
from app.api.chat_routes import router as chat_router, mount_web
from app.api.ingest_routes import router as ingest_router
app.include_router(chat_router); app.include_router(ingest_router); mount_web(app)   # mount_web last
```

## `POST /chat` → `text/event-stream`

Request body (JSON):

| Field | Type | Notes |
| --- | --- | --- |
| `question` | string | required |
| `session_id` | string \| null | the page keeps one per tab and sends it every time |
| `graph` | string \| null | code graph name, for example `code_fema356` |
| `facts` | list \| null | only on a re-run from the facts panel: the full edited facts list (shape below). The agent writes them as `ASSUMES` edges before answering |
| `rerun` | bool | `true` when the facts panel re-runs the last question (the answer should start with the `Changed:` line, SPEC Step 6) |
| `mock` | bool | `true` forces the mock replay even when the agent is available |

Response headers: `X-Chat-Mode: agent | mock`, `X-Code-Graph: <graph name>` (the page uses
it for `/node` lookups when it was not told a graph).

Each event is a standard SSE frame, the payload is JSON:

```
event: tool_call
data: {"id": "t1", "name": "find_entry", "args": {"query": "pseudo lateral load"}}

```

### Event types

**`tool_call`** — Claude asked for a tool. Sent before the tool runs.

```json
{"id": "t3", "name": "get_dependencies", "args": {"parameter": "V"}}
```

`id` is the tool-use id (any string unique within the answer).

**`touched`** — what a tool result touched. One per tool result (may be empty lists).
This is the tool result's `touched` field plus two optional keys.

```json
{
  "tool_call_id": "t3",
  "summary": "V is computed by Equation 3-10 from 6 inputs; C_m comes from Table 3-1",
  "nodes": [{"label": "Parameter", "id": "C_m", "name": "C_m", "page": 101}],
  "edges": ["Equation 3-10|USES|C_m", "Table 3-1|GIVES|C_m"]
}
```

- `nodes[].label` is the FalkorDB label, `nodes[].id` the node's `id` property. Both are
  required. `name` (short display text) and `page` are optional; the page derives a name
  from the id when `name` is missing.
- `edges[]` are `"src|TYPE|dst"` with **bare node ids** (`id` property, no label). The
  page resolves each end against nodes it has already seen in this answer; send the node
  in the same or an earlier `touched` event. Ids must not contain `|`.
- `tool_call_id` and `summary` are optional; `summary` is the tool result's one-line
  summary and is shown under the tool step in the chat.

**`answer_delta`** — answer text, streamed.

```json
{"text": "V = 1,944 kips "}
```

**`answer_done`** — the answer is complete.

```json
{
  "path": {
    "nodes": [{"label": "Equation", "id": "Equation 3-10"}],
    "edges": ["3.3.1.3.1|CONTAINS|Equation 3-10", "Equation 3-10|COMPUTES|V"]
  },
  "labels": [
    {"node": {"label": "Table", "id": "Table 3-1"}, "tag": "unreviewed", "note": "not yet approved in the review queue"},
    {"edge": "Table 3-1|GIVES|C_m", "tag": "inferred", "note": "global-pass edge, confidence 0.86"}
  ],
  "citations": [{"label": "Equation", "id": "Equation 3-10", "text": "Eq. 3-10", "page": 101}],
  "cost": {"usd": 0.0412, "input_tokens": 18240, "output_tokens": 912, "tool_calls": 6, "model": "claude-sonnet-5-5"}
}
```

- `path` is the final answer path (what `trace` returns); those nodes and edges turn bold,
  everything else touched stays faint. Path nodes not seen before are added.
- `labels[]`: `tag` is one of `unreviewed`, `inferred`, `assumed`, `flagged`; each entry
  targets a `node` or an `edge`. Unreviewed/flagged nodes get a dashed border, inferred
  edges a dashed line.
- `citations[]` give chip text and page for the citation markers in the answer (below).
  Optional; without it chips use the touched node's `name`/`page`.
- `cost`: all keys optional.

**`facts`** — the session's current facts (the full list, replaces the panel). Send after
`save_session_facts`, after a row match, and once more before `answer_done` if anything
changed.

```json
{"facts": [
  {"param_id": "W", "symbol": "W", "latex": "W", "value": 2400, "unit": "kips", "source": "user"},
  {"param_id": "C_m", "symbol": "C_m", "latex": "C_m", "value": 0.9, "unit": null,
   "source": "matched_row", "note": "Table 3-1, 3 or more stories, concrete moment frame",
   "node": {"label": "Row", "id": "Table 3-1#r4"}}
]}
```

- `source` is `user`, `matched_row` or `assumed` (the `ASSUMES` edge's source, SPEC Step 6).
- `value` is a number or a string (for example a performance level). `latex`, `note` and
  `node` are optional; `node` makes the fact's tag clickable (opens the source panel).
- Editing a value in the panel sets that fact's `source` to `user` and re-sends the last
  question with the whole list in `facts` and `rerun: true`.

**`error`** — something failed; the page shows `message` in the chat and stops.

```json
{"message": "FalkorDB is not reachable"}
```

### Citation markers in answer text

The answer cites nodes inline as `[[Label:id]]`, for example
`[[Equation:Equation 3-10]]`, `[[Clause:3.3.1.3.1]]`, `[[Row:Table 3-1#r4]]`. The label is
everything before the first `:`. The page turns each marker into a clickable chip that opens
the source panel and highlights the node. Put this format in the agent's system prompt.

The rest of the answer is light Markdown: paragraphs, `- ` lists, `**bold**`, `` `code` ``
and inline math `$...$` (rendered with KaTeX).

## Agent hook

`POST /chat` streams from the real agent when this import works:

```python
from app.agent.stream import stream_chat
```

```python
def stream_chat(question: str, *, session_id: str | None, graph: str | None,
                facts: list[dict] | None, rerun: bool) -> Iterator[dict] | AsyncIterator[dict]:
    """Yield {"event": <type>, "data": <payload>} dicts, in the order above."""
```

Sync or async generators both work. If the import fails, or the request has `mock: true`,
the route replays the built-in mock (an Equation 3-10 question on `code_fema356`).

## `GET /node/{label}/{id}` → JSON

Source panel data, read-only. `id` is URL-encoded (`Table%203-1%23r4`). Query param
`graph` (else the `CODE_GRAPH` env var). `label` must be one of the schema's node labels.

```json
{
  "label": "Equation", "id": "Equation 3-10", "title": "Equation 3-10",
  "page": 101, "page_end": null, "status": "extracted", "review_status": "unreviewed",
  "confidence": 0.9, "extraction_method": "claude:...",
  "text": "...",                 // clause text (corrected if available), row cells, definition
  "latex": "V = C_1 C_2 C_3 C_m S_a W", "sympy": "...",
  "crops": ["/crop/code_fema356/equations/Equation_3-10_p101.png"],
  "pdf_url": "/pdf/code_fema356#page=101",
  "flags": [{"reason": "unmapped", "severity": "low", "detail": "..."}],
  "where": [{"symbol": "Cm", "definition": "..."}],
  "rows": [{"id": "Table 3-1#r3", "cells": ["1–2", "1.0"]}],
  "related": [{"dir": "out", "type": "COMPUTES", "label": "Parameter", "id": "V", "name": "V"}],
  "props": {}
}
```

Keys that do not apply are `null` or empty.

## `GET /chat/example` → JSON

`{"question": "...", "graph": "code_fema356"}`: the example question on the empty chat (the
mock replay's question). Open the page with `?mock` to force the mock while the agent is
wired in, and `?graph=<name>` to pin the code graph.

## `GET /crop/{path}`

Serves image files under `data/crops/`, for example
`/crop/code_fema356/equations/Equation_3-10_p101.png`. Node crop paths are stored as
`data/crops/...`; `/node` already rewrites them to `/crop/...` URLs.

## `GET /pdf/{graph}`

Serves the PDF a code graph was built from (its `Document` node's path), so the source
panel can link `#page=N`.

## Library and ingestion routes (`app/api/ingest_routes.py`)

| Route | What it does |
| --- | --- |
| `GET /codes` | One card per `code_*` graph: title, edition, pages, status, counts, symbol warnings; plus running upload jobs |
| `POST /codes?filename=x.pdf` | Body is the PDF (`Content-Type: application/pdf`). Same hash already ingested → `{"status": "exists", "graph"}`; over 1,000 pages or mostly without a text layer → 422; else stores it in `data/uploads/` and starts a job → `{"status": "started", "job"}` |
| `POST /codes/{graph}/reingest` | New job on the code's stored PDF; confirming it deletes and rebuilds the graph |
| `GET /jobs/{id}` | Job stage and, once ready, the profile view (metadata, streams, sample headings, validation) |
| `POST /jobs/{id}/confirm` | `{title, edition, jurisdiction}` → saves them to the profile, runs Pass 1 (no Docling) then Pass 2 with `max_cost = UPLOAD_MAX_COST` (default 3) |
| `GET /jobs/{id}/events?after=N` | SSE, numbered ids, replayable: `stage` {stage, message, t}, `log` {message, t}, `counts` (pass 1 counts, or pass 2 {done, total, flagged, cost_usd, max_cost}), `graph` (clauses of depth ≤ 2 as they are written: {nodes, edges}), `graph_status` ({id, status} per finished clause), `end` |
| `GET /codes/{graph}/report` | Ingestion report: node counts, coverage, flags by reason and severity, symbol warnings by page, Pass 1 / Pass 2 / upload run reports (cost, time) |

Jobs live in memory: a server restart forgets running jobs (the graph and reports stay).

## Recent chats (`app/api/session_routes.py`, read-only)

| Route | What it does |
| --- | --- |
| `GET /codes/{graph}/sessions?limit=3` | Sessions of that code in the memory graph (`MEMORY_GRAPH` from `app/memory/graph.py`), newest first, those with at least one question: `{id, started, last, questions, first_question}` |
| `GET /sessions/{id}` | `{graph, messages: [{role, text}], facts: [...facts panel shape], matched_rows, last_question}` |

The chat page opened as `chat.html?graph=G&session=ID` shows the stored messages and facts and
sends `session_id: ID` with the next question, so `stream_chat` should continue that session
(`MemoryGraph(db, graph, session_id=ID)`) rather than start a new one.
