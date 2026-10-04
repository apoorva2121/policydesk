# PolicyDesk

A learning project: an **agentic RAG** assistant that answers questions about a
company's expense policy *and* its actual expense data.

It is built step by step, to be understood — not to look impressive.

## What problem is this solving?

Imagine a company. It has:

- **Policy documents** (PDFs): "international travel per-diem is capped at $85/day".
- **A spend database** (SQL table): every expense anyone submitted.
- **Spreadsheets** (Excel): expense reports people upload.

Now look at four questions an employee might ask:

| Question | What is needed |
| --- | --- |
| "What is the per-diem limit for international travel?" | Read the PDF. Ordinary RAG is enough. |
| "What did Engineering spend on travel in Q3?" | Run a SQL query. The AI must **not** invent this number. |
| "Which of Priya's October expenses break policy?" | **Both** — the limit from the PDF *and* the rows from SQL, then compare. |
| "What is our parental leave policy?" | Nothing. It is not in our documents. The honest answer is "I do not know". |

The third question is the whole point of this project. No single search can
answer it. Something has to *decide* to look in two places and then reason about
what it found. That something is the agent.

## Plain-language definitions

- **RAG** (Retrieval Augmented Generation): before the AI answers, we search our
  own documents and paste the relevant pieces into the question. The AI answers
  from those pieces instead of from memory. This is how you get answers about
  *your* data, with sources.
- **Agentic RAG**: the AI also *decides* how to search — which source to use,
  whether to rephrase the question, whether what came back is good enough, and
  whether to try again. In ordinary RAG those steps are fixed. In agentic RAG
  they are decisions made while the program runs.

Every other term is in `docs/GLOSSARY.md`.

## Current status

| Milestone | Status |
| --- | --- |
| 0. Project skeleton, config, secrets, logging, tests | **done** |
| 1. Sample data + PDF/Excel extraction | **done** |
| 2. Plain RAG baseline (chunking, embeddings, citations, refusal) | **done** |
| 3. Tools, text-to-SQL, four safety layers | **done** |
| 4. LangGraph orchestration (router, planner, retry loop, approval) | next |
| 5–10 | see `docs/PLAN.md` |

## Setup from a clean machine

You need: Python 3 (we create our own 3.12 underneath), `git`, and
[uv](https://docs.astral.sh/uv/) for installing packages.

```powershell
# 1. Create an isolated Python 3.12 environment inside the project
uv venv --python 3.12 .venv

# 2. Install the project and its development tools
uv pip install -e .
uv pip install pytest ruff

# 3. Create your settings file (it is git-ignored, so your key stays private)
Copy-Item .env.example .env
# Now open .env and paste your OpenAI key into OPENAI_API_KEY

# 4. Check everything is wired up
.venv\Scripts\python.exe -m policydesk.cli doctor
```

Expected output of step 4: a table of settings, ending in `Status: READY`.

### Running without an API key

Everything that does not call a model works in test mode:

```powershell
$env:APP_ENV="test"
.venv\Scripts\python.exe -m policydesk.cli doctor
```

## Everyday commands

| What you want | Command (PowerShell) |
| --- | --- |
| Health check | `.venv\Scripts\python.exe -m policydesk.cli doctor` |
| Build the sample data | `.venv\Scripts\python.exe -m policydesk.cli generate-data` |
| See what extraction produced | `.venv\Scripts\python.exe -m policydesk.cli extract` |
| Look inside the database | `.venv\Scripts\python.exe -m policydesk.cli inspect-db` |
| Estimate the project's model cost | `.venv\Scripts\python.exe -m policydesk.cli cost` |
| See how documents were chunked (free) | `.venv\Scripts\python.exe -m policydesk.cli chunks` |
| Build the vector index | `.venv\Scripts\python.exe -m policydesk.cli index` |
| Ask a question | `.venv\Scripts\python.exe -m policydesk.cli ask "your question"` |
| Ask, showing retrieved chunks | `... ask "your question" --show-chunks` |
| Clear the model response cache | `.venv\Scripts\python.exe -m policydesk.cli cache-clear` |
| Run tests | `.venv\Scripts\python.exe -m pytest` |
| Lint | `.venv\Scripts\python.exe -m ruff check .` |
| Auto-format | `.venv\Scripts\python.exe -m ruff format .` |

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Configuration problem. The app did not start.` | No `.env`, or `OPENAI_API_KEY` is empty | `Copy-Item .env.example .env`, then paste your key |
| `ModuleNotFoundError: No module named 'policydesk'` | Package not installed into the venv | `uv pip install -e .` |
| Tests fail asking for a key | Something set `APP_ENV` in your shell | Open a new terminal; tests force `APP_ENV=test` themselves |
| `uv: command not found` | `uv` is not on PATH | `pip install uv`, or use `python -m pip install -e .` instead |

## What this costs to run

Run `policydesk cost` for the current estimate with all assumptions shown.

As of 4 October 2026, using mini models throughout: **about $3 for the entire
project**, of which roughly two thirds is the evaluation milestone re-running its
question set. Switching the answer and verification steps to `gpt-4o` raises that
to about $31 — which is exactly why we measure the quality difference before
deciding it is worth paying for.

Two separate protections:

1. `MAX_SPEND_USD_PER_RUN` in `.env` stops a single run that goes over budget.
   This is a seatbelt for *our* code, and a bug could get around it.
2. A monthly usage limit set in your OpenAI dashboard. This one cannot be
   bypassed by anything we write. **Set it.** $10 is more than enough here.

## Honest limitations

This is a **learning system**, not a production one. Having Docker, tests and
tracing does *not* make software production-ready. `docs/PLAN.md` keeps a running
list of what a real deployment would additionally need.

## Documentation map

- `docs/PLAN.md` — the full milestone plan and architecture
- `docs/PROGRESS.md` — what is done, what was decided, what is next
- `docs/GLOSSARY.md` — every technical term, explained once, simply
