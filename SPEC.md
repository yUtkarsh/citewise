# Code Graph Agent: Detailed Flow Spec

Oct 17, 2026 · @Utkarsh

## Overview

The product turns any digital building-code PDF into a FalkorDB graph, then lets an agent answer design questions with a traced, clause-level path through that graph. Built solo for Graph Hacks (WeMakeDevs x FalkorDB), deadline Oct 18, 2026, 11:59 PM IST.

**Core principle.** Claude makes judgments that generalize (rules, meanings, symbols in images). Deterministic code applies them and verifies the results. FalkorDB holds every fact the agent uses.

| Area | Decision |
| --- | --- |
| Tracks entered | Agents That Act on Connected Data; Company Brain; Agent Memory and Coordination if memory layers ship |
| Backend | Python, FastAPI, Server-Sent Events to the frontend |
| Frontend | Simple web page, Cytoscape.js for the graph, KaTeX for equations |
| Graph database | FalkorDB via Docker; one graph per code document, one memory graph per user |
| LLMs | Claude API: Haiku for bulk extraction, Sonnet for the chat agent |
| PDF tools | PyMuPDF for text, fonts and crops; Docling only on table pages |
| Embeddings | Local sentence-transformers model |
| Billing | Console API credits bought separately (Max plan credits unlock after 7 days, too late) |

**Supported input.** Digital PDFs with a Unicode text layer.

**Known limitations.** Scanned PDFs are not supported. Math constructions (primes, radicals, large brackets, stacked fractions) can extract wrongly from the text layer; equations and table headers are therefore read from image crops, and symbol-font problems are reported as warnings.

**Data rule.** Demo documents must be public (FEMA 356 as primary). Licensed codes such as ASCE 41 are never loaded into the public demo or repo.

## Step 1: Library and upload

The app opens on a library of ingested codes; the user picks one to chat with or uploads a new PDF. Single user, no login.

**Library screen**

- One card per code: title, edition, jurisdiction, page count, ingestion status, symbol-warning count.
- Card actions: open chat, view ingestion report, re-ingest, delete.
- One chat queries exactly one code. Multi-code chat is out of scope.

**Upload, in order**

1. Hash the file. If the hash already exists, open the existing card instead of re-ingesting.
2. Reject files over about 1,000 pages for the hackathon build.
3. Reject scanned PDFs: if most pages have no text layer, show a message that only digital PDFs are supported.
4. Store the file and create an empty FalkorDB graph for it.
5. Hand off to Step 2. Title, edition and jurisdiction are detected by Claude there and confirmed by the user on the profile screen.

**Memory.** With a single user there is one memory graph, holding one node per chat session.

## Step 2: Document profile

Claude decides the rules for this document; deterministic code checks them on every body page; the user confirms. Target time: under a minute for a 900-page code.

**1. Page map \[deterministic\].** One line per page from raw PyMuPDF spans: first heading-like text, line count, count of lines starting with a number, dominant heading styles, dot leaders present, tables or figures present.

**2. Range labelling \[Claude\].** Claude reads the whole page map and labels page ranges: front matter, notation, definitions, body, commentary, appendices, index. It picks about 8 sample pages across chapters, including one with a table and one with equations.

**3. Rule proposal \[Claude\].** From the sample pages, Claude returns the profile:

- Heading streams, each with a number pattern and heading style (for example provisions `2.4.1` and commentary `C2.4.1`).
- Hierarchy levels, caption formats for tables and figures, equation-number format, footnote format (how footnote lines and markers look).
- Running elements to strip: headers, footers, page numbers, license watermarks.
- Title, edition and jurisdiction from the title pages.

**4. Validation \[deterministic\].** Apply the profile to every body page and check: order within each stream, hierarchy consistency, and at least 90% of body text assigned to a clause. On failure, Claude gets the failing spots and revises once. If it still fails, ingestion proceeds with a warning; the low-coverage page ranges are listed in the ingestion report.

**5. Confirmation screen \[user\].** Shows the detected title, edition and jurisdiction (editable), about ten sample headings spread across the document, and suspicious spots such as long runs of pages with no headings.

**Teach by example.** If a heading was missed, the user clicks it in the PDF preview. The backend reads that span's style and number format, updates the profile, and re-runs validation. One click fixes the rule for the whole document.

**Commentary.** Commentary is extracted in full, like provisions. Pass 1 links each commentary clause to its provision by number (`C2.4.1` to `2.4.1`, edge `COMMENTARY_ON`). Unmatched commentary is linked to provisions later, in the global pass.

## Step 3a: Ingestion pass 1, skeleton

Pass 1 is fully deterministic and builds a navigable graph of the document in about a minute. Chat works for navigation questions once it finishes.

1. **Extract blocks.** PyMuPDF reads every body page. Docling runs only on pages the page map flagged for tables and builds their grids; PyMuPDF `find_tables()` is the fallback when Docling fails on a layout. Output is one ordered list of blocks: `page, bbox, col, text, style, kind`, plus kind-specific fields (heading number and stream, table rows, footnote marker, equation number and raw spans, where-block items, links).
2. **Clean and order.** Strip running elements from the profile. Order two-column pages by column, then top to bottom. Re-join hyphenated words; normalize ligatures, minus signs and primes in body text.
3. **Segment into clauses.** Walk blocks in order; each heading matching a profile stream starts a clause. Split headings that run into body text on the same line. Text before a page's first heading continues the previous clause. Hierarchy comes from numbering (`PART_OF`); commentary links by prefix (`COMMENTARY_ON`).
4. **Build objects.** Tables and rows from grids, with footnotes attached by marker. Equations from right-aligned numbers, assigned to their clause. Figures paired with captions. Inline where-blocks parsed into symbol, definition and unit.
5. **Crop images.** Equations (box expanded to cover stacked fractions and nearby spans), whole tables, and table header rows, rendered at 200 DPI and stored on their nodes. Figures are cropped for display only. Clause crops are never stored. They are rendered on the fly from the PDF using the clause’s page and bbox, sent with the Pass 2 call, and discarded. Store the PDF, positions and results; render images when needed. Equation and table crops stay stored, since the UI shows them.
6. **Merge multi-page tables.** Merge when the next page starts with a "continued" caption, or with no caption and matching column count and positions. Drop repeated header rows. Conflicting signals are flagged, never guessed.
7. **Find references.** PDF link annotations first, then regex, including lists and ranges. Each reference resolves to a node ID. References to other codes become `ExternalCode` nodes (for example ACI 318-19), left as nodes only: they mark where a future agent for that code would be called.
8. **Symbol warnings.** Flag a line when it contains private-use characters (U+E000 to U+F8FF), control characters including null bytes, or U+FFFD; when an ASCII letter or digit comes from a symbol or math font (a likely unmapped symbol); or when a superscript follows a letter and no footnote with that marker exists in its scope (the page, or the whole merged table for markers inside a multi-page table). Using a symbol font is not a warning by itself. Flagged lines are recorded per page for the ingestion report, with their bbox; Pass 2 renders their line crops on the fly.
9. **Write to FalkorDB.** Create indexes first (`Clause.id`, `Parameter.symbol`, full-text on clause text), then batched `UNWIND … MERGE` writes so re-runs never duplicate.

**Live view.** During Pass 1 the graph view shows chapters and sections only, appearing as they are written, with a progress bar.

## Step 3b: Ingestion pass 2, semantics

Pass 2 uses Claude to turn the skeleton into a computable graph, one validated call at a time, writing each result as soon as it passes.

**Models, set in `.env`**

| Variable | Default | Used for |
| --- | --- | --- |
| `MODEL_NOTATION` | Sonnet | Notation and definitions pages |
| `MODEL_TABLES` | Sonnet | Table calls |
| `MODEL_CLAUSES` | Haiku | Clause calls and schema discovery |
| `MODEL_AGENT` | Sonnet | Chat agent (Step 5) |

All calls are real-time, 10 to 20 in parallel. Extraction calls use a low effort setting to limit output tokens. The Batch API is not used: it saves only a few dollars per ingestion and its results can take hours.

**Order of jobs**

1. **Schema discovery (automatic, every upload).** Claude reviews about 5% of clauses, sampled across chapters, and proposes concepts the core schema lacks. Proposals are auto-accepted only into the open slots: new `Category` types (for example `DiaphragmType`, `SoilType`) and new `REFERENCES` types. Proposals for new structural node or edge types are never applied automatically; they are listed in the ingestion report. The schema is then frozen and versioned for this document.
2. **Notation pages.** One call per page with the full page image plus its text. Each entry returns display symbol, LaTeX, a stable id (for example `f_prime_cE`), description, units, `defined_in` references, aliases and a source quote. A symbol listed twice with different meanings becomes two nodes, each scoped to its chapters.
3. **Definitions.** Terms become `Term` nodes linked to the clauses that use them.
4. **Clauses, one call each.** Input: breadcrumb, clause text, local where-block, the notation entries whose symbols appear in the clause, outgoing references with target titles, the clause's equation crops, and a crop of the whole clause region (one per page the clause spans), controlled by `CLAUSE_CROPS=always|flagged` in `.env`, default `always`. With `flagged`, only lines flagged in Pass 1 are cropped. Output: parameters defined, equations (LaTeX, SymPy form, computes, uses), conditions, requirements, exceptions, typed references, and an `unmapped` field for anything outside the schema. Claude compares the text with the image and corrects misread symbols only. Validation accepts character-level corrections (for example ‘p’ to ‘√’, ‘0’ to ‘′’). Everything else must still match the text layer, or the result is rejected and retried. Each correction is stored with the raw text and the corrected text. Corrections Claude marks as uncertain go to the review queue.
5. **Tables, one call each.** Input: table crops in page order, Docling grid JSON, caption, footnotes, notation slice, and the referencing clause's text. Output: header parameters, row conditions, values with units, formula cells, footnote rules, and blank-cell interpretations (`applies_to_rows`). Very large tables are split by page segment with header context repeated.

**Validation per result**

- Output matches the Pydantic schema; every source quote appears in the source text.
- Equations parse in SymPy; every symbol is defined locally or in the notation dictionary; letters roughly match the text layer.
- Table numbers match Docling cells exactly; grid dimensions match; header cells are exempt.
- Source trust, decided per table:
  - **Text clean** (no symbol warnings in the table region and a text layer present): Claude's numbers must match the text layer; a mismatch is retried, then flagged.
  - **Text suspect or missing** (symbol warnings or unmapped-font spans in the region, or no text layer): Claude's numbers are accepted from the image, and each such value is marked `source: image`. Only those cells are sent for a second, independent read with the same model. If both reads agree, the value is stored with a low-severity note; if they disagree, it is flagged high.
- Every blank-cell interpretation is flagged for review.
- On failure, one retry with the error in the prompt; then the result is stored as flagged.

**Delivery.** Results are cached by hash of source text, schema version and prompt version. Each clause and table is written to FalkorDB as soon as it validates, moving status from `skeleton` to `extracted` or `flagged`. Job state is saved, so a crash resumes where it stopped.

## Step 3c: Global pass and ingestion report

After every clause and table is extracted, one global pass cleans up the graph as a whole, then the ingestion report summarizes quality and cost.

**Global pass, in order**

1. **Entity resolution, three tiers.** First, exact symbol match against the notation dictionary. Second, similarity on names and aliases (local embeddings) to find likely duplicates. Third, Claude adjudicates each ambiguous cluster: merge, keep separate, or flag. Merges keep every source link.
2. **Inferred edges.** Claude proposes connections the code implies but never states, for example `Table 16-3 GIVES v_u` (same meaning, same units). Each carries a confidence score and `method: inferred`. The agent may use them, and must label them in answers as inferred, not stated by the code.
3. **Unmatched commentary.** Commentary not linked by number prefix is linked to provisions by Claude, as inferred edges.
4. **Graph-wide checks in Cypher.** Parameters used but never defined; references to missing clauses; cycles in `COMPUTES` or `USES`. Every hit goes to the review queue.
5. **Embeddings.** Clause summaries, parameter definitions, aliases and term definitions are embedded with a local sentence-transformers model and stored in a FalkorDB vector index for `find_entry`.

**Not built: shared categories across tables.** Rows from different tables describing the same thing (for example diaphragm types in Tables 16-3 and 16-4) are not pre-linked. The agent matches rows to the user's description at question time and explains its choice. Each match is stored in the answer trace, so follow-up questions in a session reuse the same rows.

**Ingestion report**

- Counts: clauses, tables, equations, figures, parameters, terms, external codes.
- Coverage from Step 2, and any low-coverage page ranges.
- Flagged items by type, with links into the review queue.
- Schema proposals from discovery: accepted and not applied.
- Symbol warnings by page.
- API cost and run time.

## Step 4: Review queue

The review queue is where the engineer checks what the system was unsure about; nothing in it blocks the chat. Flagged items stay usable by the agent, labelled as unreviewed in answers.

**Severity, highest first**

| Severity | Item types | Why |
| --- | --- | --- |
| High | Table number mismatch, equation text-vs-image mismatch, SymPy parse failure, undefined symbol in an equation, blank-cell interpretation | Can change a computed result |
| Medium | Inferred edges, multi-page merge conflicts, entity-resolution adjudications, references to missing clauses, uncertain symbol correction | Can change which provision applies |
| Low | Failed source quotes on descriptive text, figures, `unmapped` fields, schema proposals not applied | Affects explanations, not numbers |

Within a severity level, items are ordered by page.

**Each item shows** the extracted result, its source (clause text, or the crop beside rendered LaTeX or parsed rows), the reason it was flagged, and its page with a link to the PDF location.

**Actions**

- **Approve.** Sets `review_status: approved`; the unreviewed label disappears from answers.
- **Reject.** The reviewer adds an optional note on what is wrong. The item goes back to Claude with the original inputs, the rejected output and the note, using `MODEL_REVIEW` from `.env` (default Sonnet). The new result passes the same validation and returns to the queue for approval. After two rejections the item is marked `needs_manual` and excluded from answers.

Every decision is stored on the node: status, time and note.

## Step 5: Chat agent and live graph view

The agent answers questions about one code by calling tools that query FalkorDB; every number in an answer comes from a tool result, and the graph view shows the path as it is walked.

**Tools (plain Python functions; MCP wrapper later if time allows)**

| Tool | What it does |
| --- | --- |
| `find_entry` | Exact match on ids and symbols, full-text search, and vector search over aliases; returns ranked candidate nodes |
| `get_dependencies` | Backward traversal from a parameter to its equations, inputs, tables and clauses |
| `applicable_clauses` | Filters clauses by triggers and conditions given the session facts |
| `get_clause` | Clause text, page, crops, references, review status |
| `compute` | Evaluates equations in SymPy; applies table row conditions and footnote interpolation rules |
| `save_session_facts` | Writes user inputs to the memory graph |
| `trace` | Formats the answer path into clause, table and page citations |
| `run_cypher` | Free-form query for questions the fixed tools miss |

**`run_cypher` guardrails.** Runs through FalkorDB's read-only query mode, so writes are impossible. Results capped at 200 rows; query timeout of a few seconds; the code graph only, never the memory graph.

**The loop.** The backend sends Claude (`MODEL_AGENT`) the history, session facts and tool definitions. Claude requests a tool; the backend runs it, streams the touched node and edge ids to the frontend, and returns the result. This repeats until Claude writes the answer.

**Answer rules (system prompt)**

- Every number, clause id and equation comes from a tool result; Claude never does arithmetic itself.
- Cite clause, table or equation, and page, for every claim.
- Label inferred edges as inferred and flagged items as unreviewed.
- When a row is matched to the user's description, say which row and why.
- Missing input: ask the user, and offer a typical value labelled as an engineering assumption, not from the code. It is used only if the user accepts it, and is stored as an assumed fact.
- If the graph lacks something, say so instead of answering from memory.

**Screen layout**

- Left: chat.
- Right: working-set graph (Cytoscape.js), empty at the start of each question. Explored nodes appear faint; the final answer path turns bold.
- Source panel on node click: clause text and page, a link to the PDF location, and for equations the crop beside the rendered LaTeX (KaTeX).

## Step 6: Memory

The Claude API is stateless, so the backend owns all memory: the conversation is resent each call, and the structured facts live in a FalkorDB memory graph that the user can see and edit.

**Memory graph (one, single user)**

- `Session` and `Message` nodes hold the chat history.
- `(Session)-[:ASSUMES {value, unit, source}]->(Parameter)` holds each fact. Source is `user`, `matched_row` or `assumed` (an accepted typical value).
- `(Session)-[:MATCHED {reason}]->(Row)` records which table row the agent matched to the user's description.
- `(Session)-[:ASKED]->(Question)-[:ANSWERED_VIA]->(...)` stores each answer's path for replay and "why" questions.
- Memory nodes point to code-graph nodes by id; the code graph is never written to.

**Context per API call**

- Full message history, with tool results replaced by one-line summaries once used.
- A "current project facts" block built from `ASSUMES` edges, injected into the system prompt every call.
- Prompt caching on the system prompt and tool definitions.
- No summarization of old turns; demo chats stay short.

**Facts panel (editable).** Above the graph view, a panel lists each fact with value, unit and source tag (from you, matched row, assumed). Editing a value updates its `ASSUMES` edge and re-runs the last question automatically.

**What-if answers.** A changed fact, from chat or the panel, re-runs the stored traversal. The answer is the full new result plus one change line at the top, for example: "Changed: D 50 → 70 ft. V\_d 15,000 → 18,750 lb; governing equation switched from 16-11 to 16-10." The graph view replays the path with changed nodes highlighted.

**Not built.** Returning to past sessions (session list and resume). The memory graph already stores what it would need.

## Demo and submission plan

The demo runs on public documents only: FEMA 356 pre-ingested and reviewed for the chat, plus a short second document uploaded live to prove the pipeline works on a different format. Submission closes Oct 18, 2026, 11:59 PM IST.

**Documents**

- **Primary:** FEMA 356, ingested ahead with real-time calls, review queue worked through.
- **Live upload:** chosen by you. Criteria: publicly licensed, under about 50 pages, a different numbering and layout from FEMA. Open question: which one.

**Demo video, in order**

1. The problem: a plain LLM answering a code question misses a clause or invents a value.
2. Live upload of the second document: profile screen, a teach-by-example click, the graph growing in Pass 1 and enriching in Pass 2.
3. Ingestion report and review queue: approve one item, reject one with a note and show Claude's corrected version.
4. Chat on FEMA 356: a calculation question, the path lighting up, citations, an equation crop beside its LaTeX.
5. What-if from the facts panel: the governing equation flips, the change line explains it.
6. Close: architecture in one diagram, limitations stated plainly.

**Submission checklist**

- [ ] Public GitHub repo; no licensed code PDFs or graphs from them committed
- [ ] README: setup, graph data model, key Cypher queries, AI-use disclosure (Claude Code for development, Claude API in the product), data sources and licenses, limitations
- [ ] Docker Compose for FalkorDB plus the app, one-command start
- [ ] Demo video with a working project
- [ ] Tracks named: Agents That Act on Connected Data; Company Brain; Agent Memory and Coordination
- [ ] Read-only live demo if time allows: pre-ingested FEMA graph, upload disabled, spend cap and rate limit on the API key
- [ ] Blog post for the side prize: the problem, why graphs, the deterministic-plus-Claude pipeline, the LLM-vs-graph comparison, and who else works on this

## Build order

Build the spine end to end first (ingest FEMA, answer one traced question in a UI), then add features in order of demo value, with a hard stop for submission at 10:00 PM IST on Oct 18. Each phase ends with a checkpoint that must work before moving on.

**Working with Claude Code**

- Put this spec in the repo as `SPEC.md` (export the doc as Markdown) and a short `CLAUDE.md`: stack, folder layout, conventions, "read SPEC.md before each phase".
- One Claude Code session per phase. Start in plan mode, review the plan, then let it build.
- Test on a fixtures folder of 5–10 FEMA 356 pages: one with a table, one multi-page table, one with equations, one notation page. Licensed pages such as the ASCE 41 test page go in a git-ignored `private_fixtures/` folder only.
- Commit after every checkpoint, so a bad session can be rolled back.
- Claude Code runs on the Max plan; the app's own API key uses Console credits.

**Phases**

1. **Setup (Oct 17, about 1 h).** Repo, Docker Compose with FalkorDB, `.env` with model variables, FastAPI skeleton. Run the font check on FEMA 356. Checkpoint: FalkorDB browser opens; FEMA's font report shows what extracts cleanly.
2. **Profile and Pass 1 (Oct 17, about 4 h).** Page map, Claude profile with validation (no confirmation screen yet), blocks, clauses, Docling tables, crops, references, warnings, writes. Checkpoint: FEMA skeleton visible in the FalkorDB browser with correct clause hierarchy.
3. **Pass 2 (Oct 17 evening, about 5 h).** Notation pages, clause calls with equation crops, table calls, validation, cache, live writes. Test on fixtures, then start full FEMA ingestion before sleeping. Checkpoint: fixtures pass validation; full run started.
4. **Agent (Oct 18 morning, about 3 h).** Tools, tool loop, `compute`, memory graph with session facts. Test from a command line first. Checkpoint: the diaphragm-style calculation question answered with correct citations.
5. **Frontend (Oct 18, about 4 h).** Chat, Server-Sent Events, working-set graph, source panel with crops and KaTeX, upload and profile confirmation screen, facts panel. Checkpoint: the full demo path works in the browser.
6. **Quality layer (Oct 18 afternoon, about 2 h).** Global pass, ingestion report, review queue with severity ordering. Checkpoint: report and queue show real FEMA items.
7. **Ship (Oct 18, from 7:00 PM).** README, video, blog post, submit by 10:00 PM IST, leaving two hours of buffer.

**FEMA ingestion timing and cost.** Real-time calls, 10 to 20 in parallel, should finish FEMA 356 in under an hour. One full ingestion costs roughly $6–9 (Haiku for clauses, Sonnet for tables and notation). Budget about $25–30 of Console credits for development, two full ingestions, agent testing and the demo.

**Cut lines, dropped in this order if behind**

1. MCP wrapper and live deployment.
2. Reject-to-Claude loop (keep approve and reject only).
3. Automatic schema discovery (run the fixed schema).
4. Teach by example (keep confirm or reject).
5. Claude adjudication in entity resolution (keep exact plus similarity).
6. Editable facts panel (fall back to read-only).

Never cut: Pass 1, Pass 2 with crops, the agent with citations, the working-set graph, the demo video and README.

## Known issues

**Profile step: a rebuilt FEMA 356 profile is weaker than the committed one (Oct 17, 2026).** With the v3 rules prompt and the self-consistency checks (where-items, reference coverage, ids), the rebuilt FEMA profile passes validation but declares no heading level for depth 5. All 531 depth-5 headings are missed (1,352 of 1,883 found), and coverage stays at 97% because the depth-4 parent absorbs their text, so no check fails. Pass 1 with that profile: clauses 1,880 to 1,352; resolved references 2,678 to 2,293; coverage 99.3% to 97.3%. The committed profile (`fixtures/fema356_profile.json`) stays in use. Follow-up (built): headings at a depth the profile does not declare are now found in any of the stream's heading styles, and a level-completeness check fails validation when more than 1% of headings sit at undeclared depths. Rebuilt again from the cached v3 calls plus one revision, the profile gained the depth-5 level but dropped the depth-1 (chapter) level: 91 hierarchy violations, validation status warning, resolved references 2,678 to 2,346. Still weaker than the committed profile, which stays in use. Next step: let the revision see the levels of the previous attempt it must keep (a check that a revision does not lose headings the first proposal found).

## Known limitations (Oct 18, 2026)

- **Two-way table lookups need `calculate`.** `compute` matches rows by their conditions; it has no per-column conditions, so a table whose columns are chosen by a header value (for example FEMA Table 1-4: Fa by site class in rows and S_S ranges in columns) is read with `calculate`: the agent cites each cell with its row, column, value and header, and the note that permits interpolation, and the tool checks every citation. Where the grid merged the header cells, a header can be checked as present in the table's header but not as standing over the cited column; the result says so and is labelled "inputs selected by agent". Building column conditions into `compute` is about 2 to 3 hours plus re-extracting the affected tables.
- **Merged cells disagree between grid revision and the blank-cell reading.** When a value spans several rows (ASCE Table 7.3.2b: one IO expression for the Primary and Secondary rows), the rebuilt grid repeats it in each row while the table extraction reads the second row's cell as blank ("same as above"), so the answer fails the blank-cell check and is not kept (the grid is rolled back). Such tables need a rule both prompts share, or a manual fix.
- **Commentary linking and inferred edges are not built.** Commentary is linked to provisions by number prefix only (Step 3c.3 is not built), and the global pass's inferred edges (Step 3c.2) do not exist; the agent and labels support them, but there are none.
- **Entity resolution is exact-match only.** Parameters are resolved by id and symbol against the notation dictionary; the similarity tier and Claude's adjudication of ambiguous clusters (Step 3c.1) are not built. `find_entry` searches by full text and local embeddings, but nothing merges duplicate parameters.
- **Orphan cleanup.** `python -m scripts.cleanup_orphans --code <name>` deletes Parameters left with no edges by re-extractions (source clause, equation or table). Notation entries and Terms that nothing links to are kept as the document's dictionary and glossary.
