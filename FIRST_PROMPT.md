# First prompt for Claude Code (Phase 1: Setup)

Start a new session in the Code tab, Local, this folder, in **Plan** mode. Paste:

---

Read CLAUDE.md and SPEC.md (especially "Overview" and "Build order", Phase 1).
We are doing Phase 1 only: Setup. Do not start Phase 2.

This is a fresh Windows machine setup, native Windows with PowerShell, no WSL.
Plan the following, then wait for my approval before changing anything:

1. Check what is already installed: Git, Python 3.12, Docker Desktop. For anything
   missing, propose the winget command and wait for my approval to run it.
2. Initialize git in this folder (the .gitignore already exists).
3. Create a Python 3.12 virtual environment in .venv and a requirements.txt with:
   fastapi, uvicorn, python-dotenv, pydantic, anthropic, falkordb, pymupdf, docling,
   sympy, sentence-transformers, pytest. Install them. Docling and
   sentence-transformers pull large dependencies; tell me before starting that step.
4. Create docker-compose.yml that runs FalkorDB with ports 6379 and 3000 and a named
   volume for data. Start it and confirm the browser UI answers on localhost:3000.
5. Create the folder layout from CLAUDE.md with empty __init__.py files, and a
   FastAPI skeleton at app/api/main.py with a /health route that also checks the
   FalkorDB connection.
6. Write scripts/font_check.py: for every PDF in fixtures/ (and data/fema356.pdf if
   present), report fonts used, non-ASCII characters with their Unicode names,
   private-use characters, control characters, and small superscript digits that
   follow letters. Print a per-file summary.
7. Run the font check and show me the results.
8. Commit.

Checkpoint for this phase: FalkorDB browser opens, /health returns ok, and the font
report shows what FEMA 356 extracts cleanly. Stop there and summarize what you did,
what you installed, and anything I need to do by hand (for example, restarting after
Docker Desktop installs, or creating .env from .env.example).

---
