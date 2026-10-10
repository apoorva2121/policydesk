# PolicyDesk

An **agentic RAG** assistant that answers questions about a company's expense
policy *and* its actual expense data — built step by step, to be understood.

Everything is Python. No JavaScript, no HTML, no build step.

```
     ┌─────────────┐    ┌──────────┐    ┌─────────┐    ┌──────────┐
     │ guard_input │───▶│   plan   │───▶│ gather  │───▶│  grade   │
     └─────────────┘    └──────────┘    └─────────┘    └────┬─────┘
                             ▲                              │
                             └──────── not enough ──────────┤
                                                            ▼
     ┌──────────┐    ┌─────────────────┐    ┌──────────┐  enough
     │ finalize │◀───│ human approval  │◀───│ compose  │◀───┘
     └──────────┘    │  (graph STOPS)  │    │ + verify │
                     └─────────────────┘    └──────────┘
```

## What problem it solves

A company has policy documents (PDFs: "international per-diem is capped at USD 85"),
a spend database (every expense anyone submitted), and spreadsheets. Four questions
show why an agent is needed at all:

| Question | What it takes | Status |
| --- | --- | --- |
| "What is the per-diem limit for international travel?" | Read the PDF. Plain RAG. | **9/9** on the eval set |
| "What did Engineering spend on travel in Q3?" | A SQL query. The AI must not invent the number. | **5/5** |
| "Which of Priya's October expenses break policy?" | **Both** — the limit from the PDF *and* the rows from SQL, then compare. | **1/4** — the known weakness |
| "What is our parental leave policy?" | Nothing. Say "I do not know". | **2/2** |

The third row is the whole point: no single search can answer it. Something has to
*decide* to look in two places and reason about what it finds. That is the agent.

## Setup from a clean machine

```powershell
uv venv --python 3.12 .venv
uv pip install -e .
uv pip install pytest ruff

Copy-Item .env.example .env        # then paste your OpenAI key into OPENAI_API_KEY

.venv\Scripts\python.exe -m policydesk.cli doctor          # should say READY
.venv\Scripts\python.exe -m policydesk.cli generate-data   # build the fake company
.venv\Scripts\python.exe -m policydesk.cli index           # embed it (~$0.00004)
.venv\Scripts\python.exe -m policydesk.cli ui              # http://localhost:8501
```

Running without a key: `$env:APP_ENV="test"` makes everything that does not call a
model work, including the entire test suite.

## Commands

| | |
| --- | --- |
| `doctor` | health check; explains any configuration problem |
| `generate-data` | create the policy PDFs, spreadsheet and database |
| `extract` | show what extraction produced, and what needs human review |
| `chunks` | show how documents were split (free, no model calls) |
| `index` | embed the chunks and build the vector index |
| `ask "…"` | plain RAG, with `--show-chunks` to see why each passage matched |
| `agent "…"` | the full agent: plans, uses tools, verifies, may pause for you |
| `approve <id>` / `reject <id>` | decide on a paused action |
| `queue` | the Finance Operations review queue |
| `eval` | run the 22-case evaluation and score the system |
| `eval-compare` | compare two runs, honestly about noise |
| `cost` | what the project costs, from a dated price table |
| `diagram` | the orchestration diagram as Mermaid |
| `mcp-server` / `mcp-demo` | the MCP server, and a client that uses it |
| `serve` | the HTTP API (localhost only: there is no authentication) |
| `ui` | the Streamlit interface |
| `learn` | **step-by-step tutor in this terminal**, with file:line citations |
| `study` | the same curriculum in a browser |
| `install-shortcut` | a desktop icon that opens both web apps **and** the tutor |
| `cache-clear` | delete cached model responses |
| `langfuse-setup` | how to run Langfuse locally, and whether it is reachable |

## What it actually costs

Measured, not estimated: **a full 22-case evaluation costs $0.017**, and a re-run
with a warm cache costs **$0.0000**. The whole project, built from scratch, is under
$0.50 of model usage.

Set a monthly usage limit in your OpenAI dashboard as well. `MAX_SPEND_USD_PER_RUN`
is a seatbelt for our own code; the dashboard limit cannot be bypassed by a bug in it.

## The measured result

```
pass rate            19/22 = 86.4%     $0.017
retrieval recall@k   0.92              search is fine
retrieval MRR        0.82              ranking is fine
tool choice          1.00              routing is perfect
cross_source         1/4               the weakness: reasoning over combined evidence
```

The most useful finding in the whole project: **gpt-4o scored identically to
gpt-4o-mini (86.4%) at 4.5× the cost.** "Use a stronger model" was an assumption,
and measuring it cost 8 cents to disprove.

## Learning it

Four ways in, sharing one curriculum and one progress file:

```powershell
.venv\Scripts\python.exe -m policydesk.cli install-shortcut   # a desktop icon: everything
.venv\Scripts\python.exe -m policydesk.cli learn --with-apps  # the same, from a terminal
.venv\Scripts\python.exe -m policydesk.cli learn              # the tutor alone, no tabs
.venv\Scripts\python.exe -m policydesk.cli study              # just the browser curriculum
```

**The desktop icon is the one-click route.** It starts the study interface and the
app itself, opens the **study tool** in one browser tab, and runs the tutor in the
window you clicked. The app itself is linked from the study page's sidebar.

One tab, though two servers start, and deliberately so: opening two tabs at once
produced two tabs showing the *same* app and no study tool, because Windows hands a
URL to the shell without waiting for the browser to be ready. One open cannot race
with anything. `--open-both` restores the second tab if you prefer it.

Anything already listening on those ports is reused and left alone. When you quit the
tutor it asks whether to leave the web apps running, so closing the tutor does not
kill the page you were reading.

The tutor prints the **real source** with the **exact file and line** to open, asks
you to write an answer before showing you the right one, and offers to run the real
command. 7 guided sessions, all 33 files, and 14 scored interview questions.

## Documentation

- `docs/PLAN.md` — the architecture and the ten milestones
- `docs/PROGRESS.md` — every milestone, every decision, and **every bug found**, with why
- `docs/GLOSSARY.md` — every technical term, explained once, simply
- `docs/DEPLOYMENT.md` — how to run it, and the honest production gap list
- `docs/AZURE.md` — the same design on Azure, with a cost shape

## Honest limitations

This is a **learning system**. Docker, tests and tracing do not make software
production-ready.

- **No authentication.** The blocking gap. Document permissions must be enforced at
  retrieval time against a known user, and a service with no identity cannot do that.
- **Answer quality on cross-source questions is 1 in 4**, measured.
- Chroma on local disk is not a production vector store; SQLite is a file.
- No OCR: scanned documents are detected and reported, never read.
- The injection scanner catches obvious cases. It is not a boundary — the real
  protections are the read-only database, the tool allow-list and the human gate.
- The Docker image build has not been verified locally.

The full list is in `docs/DEPLOYMENT.md`, ordered by how soon it would hurt.

## Licence

[MIT](LICENSE). The policy PDFs and expense database in this repository describe a
fictional company and contain no real data.
