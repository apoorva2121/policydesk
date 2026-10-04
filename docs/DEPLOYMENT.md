# Running it, and what "production-ready" would actually require

## Running it locally

Four commands, in order. The first three are one-off; the fourth is the app.

```powershell
# 1. Create the fake company's documents and database
.venv\Scripts\python.exe -m policydesk.cli generate-data

# 2. Embed the documents into the search index (costs about $0.00004)
.venv\Scripts\python.exe -m policydesk.cli index

# 3. Check everything is wired up
.venv\Scripts\python.exe -m policydesk.cli doctor

# 4. Pick an interface
.venv\Scripts\python.exe -m policydesk.cli ui        # Streamlit, http://localhost:8501
.venv\Scripts\python.exe -m policydesk.cli serve     # HTTP API, http://localhost:8000/docs
.venv\Scripts\python.exe -m policydesk.cli agent "your question"   # the terminal
```

## Configuration by environment

| | dev | test | prod |
| --- | --- | --- | --- |
| `APP_ENV` | `dev` | `test` | `prod` |
| API key required | yes | **no** | yes |
| Logs | colourful, for humans | quiet | `LOG_JSON=true`, one object per line |
| Response cache | on (saves money while iterating) | on | **consider off** — see below |
| Spend cap per run | $1.00 | n/a | set it deliberately |
| Bind address | localhost | n/a | localhost until there is authentication |

Two of those deserve explanation.

**`APP_ENV=test` skips the credential check.** That is why the entire test suite,
and all of CI, runs with no API key. A suite that needs a paid key is a suite that
gets skipped the week the key expires.

**The cache in production is a real decision, not an obvious one.** It makes
repeated questions free and instant. It also means that if a policy document
changes, the same question returns the old answer until the cache is cleared —
because the cache key is the request, and the request did not change. For a
learning project that is a good trade. For a live policy assistant it is a wrong
answer waiting to happen, and the fix is to include a version of the index in the
cache key. That is not built here; it is on the list below.

## Versioning, and how to roll back

Five things change independently in a RAG system, and each needs its own answer.
Getting this wrong is why "we changed something and it got worse, but we're not
sure what" is such a common state.

| What changes | How it is versioned | How to roll back |
| --- | --- | --- |
| **Code** | git | `git revert`, redeploy |
| **Prompts** | they live in the code, so: git | same as code — this is *why* they are in Python files and not in a database |
| **The index** | `index-manifest.json` records the embedding model, chunk strategy and sizes | `policydesk index` rebuilds it; a mismatched index **refuses to load** rather than returning nonsense |
| **The evaluation set** | git, in `evaluation/dataset.py` | a changed expectation shows up in a diff, with its reasoning |
| **Model choice** | `.env` | change one line, then **re-run the evaluation** — a model swap is a change like any other |

The index is the one people get wrong. An index built with one embedding model and
queried with another returns confident nonsense and raises nothing at all. That is
why the manifest exists and why loading checks it.

## Before deploying anything: the checklist

Run these and read the output. Every one of them has caught a real problem in this
project.

- [ ] `pytest` — all tests pass
- [ ] `ruff check .` and `ruff format --check .` — clean
- [ ] `policydesk doctor` — exits 0 and says READY
- [ ] `policydesk eval --save` — compare against the last run; look at **churn**, not
      just the delta
- [ ] `policydesk cost` — the estimate still matches the price table's date
- [ ] `policydesk extract` — no source file silently failing
- [ ] `curl /readyz` — returns 200 with every check true
- [ ] A monthly usage limit is set in the OpenAI dashboard (ours is a seatbelt; that
      one is the real cap)
- [ ] `.env` is not in the image: `docker run --rm policydesk:test cat /app/.env` must fail
- [ ] One manual question through the UI, reading the citations and the SQL

## Docker

```bash
docker build -t policydesk .
docker run --rm -p 8000:8000 \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  policydesk
```

The data directory is a volume on purpose. The index and the database are *state*:
baking them into the image would mean rebuilding it to add a document, and would
put one company's expense data inside a shareable artefact.

**Honestly recorded:** the image build has **not** been verified on this machine —
Docker Desktop was not running when the Dockerfile was written. The CI workflow
builds it on every push, which is where the first real verification will happen.
The Dockerfile is therefore best treated as reviewed-but-untested until that job
goes green.

## What "production-ready" would actually require

A Dockerfile, a test suite and a tracing integration do **not** make software
production-ready. This is the honest list of what is still missing, roughly in the
order it would have to be done.

### Blocking — would be negligent to deploy without

1. **Authentication and authorisation.** There is none. No login, no API key, no
   user identity anywhere.
2. **Document-level permissions enforced at retrieval time.** Not in the prompt —
   *in the query*. A user must be unable to retrieve a chunk they may not read, and
   that filter has to be applied by the search, against an authenticated identity.
   Asking the model not to mention it is not a control.
3. **Row-level security on the expense data.** Right now any question can read any
   employee's expenses. The allow-listed tables are hard-coded, not per-user.
4. **A real secret manager.** `.env` on disk is right for one laptop. Production
   needs injected secrets (Key Vault, Secrets Manager) and rotation.

### Serious — would hurt within weeks

5. **A managed vector store.** Chroma on local disk has no backups, no access
   control and no concurrent writers.
6. **A real database.** SQLite is a file. Concurrent writes, backups and
   point-in-time recovery all need a server.
7. **Per-user rate limiting and spend caps.** Ours are per *run*. One user in a
   loop can still run up a bill.
8. **Alerting.** Tracing shows you what happened; nothing here tells anyone that
   the refusal rate tripled this morning.
9. **Data retention and deletion.** The expense records contain personal data. How
   long are traces kept? What happens on a deletion request?
10. **Human review before anything reaches someone senior.** The review queue is a
    file. There is no reviewer workflow, no SLA, no audit of who decided what.

### Important — would be noticed in a quarter

11. **Load testing**, and a measured answer for latency and cost per question at
    the volume you actually expect.
12. **A reviewer-facing UI.** Streamlit is right for an internal tool and wrong for
    a product: no routing, a full re-run on every interaction, one user per process.
13. **Index freshness.** Nothing notices when a policy document changes. No
    re-indexing schedule, no staleness metric, no cache invalidation (see above).
14. **Answer quality.** The pass rate sits at 86.4-90.9% across runs, and
    cross-source questions at 1 to 2 of 4. `cross-01` (a false positive on a
    hotel that is within its limit) and `cross-03` fail in every run. The numbers
    are in `docs/PROGRESS.md` and `eval-runs/`, not hidden.
15. **A bigger evaluation set.** Twenty-two cases catch a regression. They cannot
    estimate a true accuracy, and nobody should quote them as if they could.

### Known limitations in the current behaviour

- Answer quality on cross-source questions is poor, and measured: 1 of 4.
- The agent only proposes an action when the question asks for one. It will not
  volunteer a flag for a breach you did not ask it to act on — a deliberate trade
  for latency, made after the tracer showed that step was 55% of wall-clock time.
- Sections spanning a page boundary lose their heading on the continuation chunk.
- No OCR: scanned documents are detected and reported, never read.
- The injection scanner is pattern-based. It catches the obvious cases; it is not a
  boundary, and the real protections are the read-only database, the tool
  allow-list and the human approval gate.
