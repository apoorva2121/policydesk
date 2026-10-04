# Progress log

Append-only. Any new session reads this file plus `PLAN.md` and is immediately
up to speed. This is how a long project survives interruptions.

---

## 2026-10-04 — Milestone 0: skeleton, config, secrets, logging, tests

**Status:** done and verified.

### What was built

| File | Purpose |
| --- | --- |
| `pyproject.toml` | Dependencies with **pinned** versions, plus pytest and ruff config |
| `.env.example` | Committed template of every setting, with blank secrets |
| `.gitignore` | Ignores `.env`, the venv, and all generated data |
| `src/policydesk/config.py` | One `Settings` class, read once, validated at startup |
| `src/policydesk/logging_setup.py` | Structured logging plus a secret-redaction safety net |
| `src/policydesk/cli.py` | `doctor` command — a health check that explains failures |
| `tests/conftest.py` | Forces every test into key-free test mode |
| `tests/test_config.py` | 6 tests: validation, fail-fast message, secret hiding, limits |
| `tests/test_logging_redaction.py` | 5 tests: known secrets, field names, key-like text, nesting, false positives |

### Verification (actually run, not assumed)

- `python -m pytest` → **11 passed**
- `python -m ruff check .` → **All checks passed**
- `python -m ruff format --check .` → **7 files already formatted**
- `python -m policydesk.cli doctor` with no `.env` → exit code **1** with the
  message `LLM_PROVIDER=openai but OPENAI_API_KEY is empty` plus the fix
- `APP_ENV=test ... doctor` → exit code **0**, `Status: READY`, and the
  deliberate fake secret printed as `demo_leak=***REDACTED***`

### Decisions and why

1. **Python 3.12, not the system 3.13.** Some AI libraries still lag behind the
   newest Python. An isolated 3.12 venv avoids install failures that have
   nothing to teach us. Created with `uv`.
2. **Versions pinned from what actually resolved**, not from memory. Recorded on
   2026-10-04. A tutorial that will not install is worthless.
3. **`SecretStr` for every credential.** It refuses to print itself, so a key
   cannot leak through a log line, a stack trace or a debug dump.
4. **Config validated at import, not at use.** Missing credentials stop the app
   immediately with the fix in the message.
5. **`APP_ENV=test` bypasses the credential check.** The test suite must pass on
   a machine with no keys at all, including in CI.
6. **Redaction is a processor, not a convention.** Three layers: exact known
   secret values, sensitive field *names*, and anything that merely looks like a
   key. One test asserts normal logs pass through unchanged, because a redactor
   that mangles ordinary logs gets switched off and then protects nothing.
7. **No field name starts with `model_`.** Pydantic reserves that prefix, so
   fields are `chat_model` and `synthesis_model`. A real trap, avoided early.

### Interview questions this milestone answers

- "How do you handle secrets and configuration in an LLM app?"
- "How do you stop sensitive data reaching your logs?"
- "What does your test suite do when no API key is available?"

### Honest limitation

Redaction is a safety net for accidents, **not** permission to log secrets, and
not a substitute for a secret manager. Milestone 9 adds the real approach
(Key Vault / environment injection at deploy time).

### Next

Milestone 1 — generate the sample dataset (policy PDFs, expense Excel files, a
SQLite expense database) and build extraction that preserves page, sheet and row
metadata, because that metadata is what makes citations possible later.

---

## 2026-10-04 — Milestone 1: sample data and extraction

**Status:** done and verified.

### What was built

| File | Purpose |
| --- | --- |
| `src/policydesk/sample_data.py` | Generates two policy PDFs, one scanned PDF, one messy Excel workbook and the SQLite database. Fully deterministic |
| `src/policydesk/ingest/models.py` | `SourceBlock` (text + where it came from) and `ExtractionReport` (what happened, with warnings separate from notes) |
| `src/policydesk/ingest/pdf_extract.py` | Per-page text, tables extracted separately, scanned pages detected |
| `src/policydesk/ingest/excel_extract.py` | Header found rather than assumed, one block per row, unresolved formulas detected |
| `src/policydesk/cli.py` | Added `generate-data`, `extract` and `inspect-db` |
| `tests/test_sample_data.py` | 6 tests: artefacts, determinism, row counts, the planted violations, the no-limits-in-DB invariant, a schema constraint |
| `tests/test_ingest.py` | 12 tests: page coverage, the policy numbers surviving extraction, table handling, scanned detection, missing file, citation format, header detection, totals row, formula warning, notes sheet, and the "every block knows its origin" invariant |

### Verification (actually run)

- `pytest` → **29 passed** (11 from Milestone 0, 18 new)
- `ruff check .` → All checks passed; `ruff format --check .` → 18 files formatted
- `generate-data` → 5 artefacts, 79 KB total, exit 0
- `extract` → 4 files, **13 blocks**; 2 files correctly flagged for human review
- `inspect-db` → 37 expenses, 5 employees, Q3 spend by department, Priya's 7 October rows

### The dataset, and the answers we now control

The policy PDF holds the limits. The database holds the transactions. **The
limits are deliberately absent from the database** — there is a test asserting
this, because if a single SQL query could answer the flagship question, the agent
would have no reason to exist.

Planted policy violations, each breaking a different section:

| Expense | Amount | Breaks |
| --- | --- | --- |
| 1101 Priya, meals, Tokyo | 112.40 | §2 international meal per diem is USD 85 |
| 1105 Priya, business class | 1,845.00 | §4 business class needs a recorded VP approval |
| 1106 Priya, software, no receipt | 89.00 | §7 receipt required at USD 75 or more |
| 2001 Daniel, client dinner | 980.00 / 5 heads | §5 cap is USD 150 per attendee |
| 2007 Daniel, dinner London | 88.50 | §2 international meal per diem |
| 4001 Tomas, hotel Madrid | submitted +84 days | §8 submit within 30 days |
| 4003 Tomas, hotel Sydney | 318.00 | §3 international hotel limit is USD 300 |
| 4007 Tomas, executive car | 210.00 | §6 premium vehicle tiers not reimbursable |

### Decisions and why

1. **Generate the data, do not download it.** Reproducible on any machine, and we
   know every correct answer — which is the only way Milestone 7's evaluation can
   mean anything.
2. **Deterministic generation, asserted by a test.** No random numbers, no "now"
   timestamps. A score you cannot reproduce is not a measurement.
3. **Location travels with the text, from the first moment.** Every `SourceBlock`
   carries file, page, sheet and row. Citations are impossible to add later if
   this is dropped during extraction, and that single mistake is the most common
   reason a document assistant gets rejected by auditors.
4. **Tables extracted separately from prose, with the header repeated on every
   row.** A retrieved row has no neighbours for context, so `112.4` is useless and
   `Amount (USD): 112.4` is an answer. Costs a few tokens, buys usability.
5. **A scanned PDF is planted on purpose, and detected.** It extracts to nothing.
   The pipeline reports it instead of silently indexing an empty document — which
   would look like a working document and answer nothing, forever.
6. **The spreadsheet is deliberately messy**: header on row 5 under a title block,
   blank spacer rows, a totals row, a formula with no cached value, and a second
   sheet with no header. All four are everyday realities.
7. **`warnings` and `notes` kept apart.** If every file produces a warning, people
   stop reading warnings, and the one that mattered slips past.
8. **Totals rows are not indexed.** A block reading `Merchant: TOTAL` can still be
   retrieved and cited. Junk in the index is worse than nothing, because it looks
   like evidence.
9. **Unresolved formulas detected by reading the file twice** (once for values,
   once for formula text). An uncalculated formula reads as *empty*, not zero.
   Treating it as zero is a confident wrong answer.
10. **A bad file is reported, never raised.** A pipeline over many files must not
    stop because one is missing or corrupt — but it must also never hide it.

### Interview questions this milestone answers

- "How do you cite at page or line level?" → the location travels with the text
  from extraction onward; citations cannot be retrofitted.
- "What do you do about scanned PDFs?" → detect, report, route to OCR as a
  separate step with its own error rate; never index an empty document.
- "How do you handle tables and spreadsheets in RAG?" → extract separately,
  repeat the header per row so a retrieved row stands alone.
- "What goes wrong in ingestion that nobody notices?" → empty extractions,
  totals rows indexed as data, uncalculated formulas read as zero.

### Honest limitations

- `pdfplumber` handles clean layouts. Multi-column pages, rotated tables and
  stamps need a stronger service (Azure AI Document Intelligence, for example).
  That trade-off belongs in Milestone 10.
- Header detection is a heuristic, which is exactly why the chosen row is
  reported rather than assumed correct.
- No OCR is installed. Detection only.
- Reading the workbook twice to find unresolved formulas doubles the read cost.
  Fine at this size; revisit if files get large.

### Next

Milestone 2 — the plain RAG baseline: chunking, embeddings, Chroma, retrieval,
a grounded answer with citations, an explicit "I do not know", and a chunk
inspector so you can see *why* a chunk was selected. This is the first milestone
that needs the OpenAI key in `.env`.


---

## 2026-10-04 — Milestone 2: plain RAG baseline, and cost control

**Status:** done and verified against the real API.

### The headline result

Four question types, asked against the real system:

| Question | Result | Why |
| --- | --- | --- |
| "What is the per-diem limit for international travel?" | **"USD 85 per day for meals [1]"**, cited to `travel-and-expense-policy.pdf (p.1, 2. Meal allowances)` | Plain RAG is enough |
| "What is our parental leave policy?" | **Honest refusal**: "Parental leave policy information is missing" | Correctly out of scope |
| "What did Engineering spend on travel in Q3 2026?" | **Refused** — "No information on Engineering's travel expenses" | Needs SQL. The numbers are in the database, which plain RAG cannot reach |
| "Which of Priya Raman's October expenses break policy?" | **Refused** — "Details of Priya Raman's October expenses are missing" | Needs the PDF limit *and* the SQL rows |

Rows 3 and 4 are the point. The system refused rather than inventing numbers,
which is the correct behaviour — and it is exactly the gap Milestones 3 and 4
close. The motivation for building an agent is now a measured fact, not a claim.

### What was built

| File | Purpose |
| --- | --- |
| `llm/cache.py` | On-disk response cache keyed by a hash of the complete request |
| `llm/client.py` | The single door to the API: token pre-check, cache, timeout, bounded retries, cost recording, specific errors |
| `rag/chunking.py` | Two strategies (`section`, `fixed`); rows and tables never split; contextual headers for embedding |
| `rag/index.py` | Chroma vector store plus a **manifest** that refuses to open a mismatched index |
| `rag/retrieve.py` | Ranked results with similarity, and `why_selected` for every one |
| `rag/answer.py` | Grounded answer with numbered citations, exact refusal token, injection framing |
| `pipeline.py` | One wiring of the stages, shared by the CLI, tests and later the API |
| `cli.py` | `chunks`, `index`, `ask --show-chunks`, `cache-clear` |
| `tests/fakes.py` | A fake client with hash-based embeddings, so tests are free and offline |
| `tests/test_chunking.py`, `tests/test_rag.py` | 35 new tests |

### Verification (actually run)

- `pytest` → **77 passed**; `ruff check .` → All checks passed
- Smoke test of the real key: embeddings 1536d, chat replied — **$0.0000028**
- `policydesk index` → 25 chunks, 1,938 tokens, **$0.00004**
- Four `ask` runs → about **$0.0001 each**
- Re-asking an identical question → **$0.0000, 100% cache hit rate**

Total real spend for the whole of Milestone 2: **under one cent.**

### Chunking: the decision, and the numbers behind it

Both strategies are implemented so the choice can be measured in Milestone 7
rather than argued about. Measured on this corpus:

| Strategy | Chunks | Median size | Largest | Chunks with a section label |
| --- | --- | --- | --- | --- |
| `section` (default) | 25 | 212 chars | 560 | 12 |
| `fixed` (900/150) | 17 | 212 chars | 877 | 0 |

`section` was chosen as the default because one chunk is one complete rule. The
meal rule holds *both* the domestic USD 60 and the international USD 85 figure;
a fixed split can separate them, and then a question about one retrieves a chunk
that does not contain the other. There is a test asserting they stay together.

### A real bug found and fixed on the way

The Milestone 1 extractor joined every line of a page into a single paragraph.
That read fine, but it **destroyed the section headings** — and the chunker needs
them. Page 1 came back as one 2,000-character blob.

Fix: `_clean()` now starts a new paragraph at every numbered heading and keeps the
heading on its own line. Page 1 became 11 paragraphs, 5 of them headings.

The lesson generalises: *extraction must preserve the structure that later stages
depend on*. Information thrown away at ingestion cannot be recovered downstream.

### Decisions and why

1. **One client class for every model call.** Every call needs a timeout, retries,
   a token count, a cost record and a cache lookup. Five copies of that means one
   of them is missing a timeout.
2. **Token count checked locally before sending.** A limit you check after paying
   is not a limit.
3. **An on-disk cache, keyed on everything that affects the answer** — model,
   messages, temperature, max tokens. Miss one field and you serve a stale answer
   after changing that very setting, then lose an hour to it.
4. **`temperature=0` everywhere.** Repeatable answers can be cached, compared and
   evaluated. Random ones cannot.
5. **Per-text embedding cache, not per-batch.** Add one document and only that one
   is embedded. Batch-level caching would re-embed everything on any change.
6. **The index carries a manifest.** An index built with one embedding model and
   queried with another returns confident nonsense and raises nothing. The
   manifest turns a silent wrong-answer bug into a clear error. Two tests cover it.
7. **Rebuild the index rather than update it.** At this size it is nearly free, and
   it removes the class of bug where deleted documents linger and get cited.
8. **Chunks are embedded with a context header**, cited without it. A chunk reading
   "The cap covers all meals..." is ambiguous alone.
9. **An exact refusal token**, not prose matching. "I do not know whether X, but Y"
   would fool any pattern match.
10. **Refuse before calling the model when retrieval is empty.** No evidence means
    no answer; paying to be told so is waste.
11. **Library HTTP loggers raised to WARNING.** They log full request URLs at INFO.
    Not logging something beats logging it and scrubbing it afterwards.
12. **Invented citations are detectable.** `[9]` when three chunks were supplied
    means the model fabricated a source; `invalid_citation_numbers` catches it.

### Interview questions this milestone answers

- "Chunk size and overlap — and why?" → strategy per document structure, with the
  meal-rule example of why a split costs you a correct answer.
- "How do you make a RAG answer auditable?" → numbered context, parsed citations
  mapped back to real chunks, invented citation numbers detected.
- "How do you know whether a wrong answer is a retrieval or a generation bug?" →
  `--show-chunks` shows what was retrieved, its score and why it matched.
- "How would you halve this pipeline's cost?" → cache, cheaper model per step,
  fewer chunks, shorter outputs (output tokens cost 4× input).
- "What happens if you change the embedding model?" → the manifest refuses to load
  the index, instead of silently returning nonsense.

### Honest limitations

- **Vector search only.** No keyword (BM25) component yet, so an exact code or
  rare identifier can be missed. Hybrid search is a later improvement.
- **No reranking.** The 5th retrieved chunk in the live run scored 0.456 with *no*
  shared words — it was included only because `top_k=5`. A similarity floor or a
  reranker would drop it.
- **Sections spanning a page boundary lose their heading on the continuation**, so
  "5. Client entertainment" appears as a 139-char chunk on p.1 plus an unlabelled
  165-char chunk on p.2. Visible in `policydesk chunks`.
- **The cache does not notice changed documents.** Edit a file, ask the same
  question, and you get the old answer until `policydesk cache-clear`.
- **Chroma on local disk is not a production vector store** — no backups, no
  access control, no concurrent writers.
- **The prompt's "treat context as data" instruction is not a security boundary.**
  It reduces injection risk; Milestone 5 adds the real controls.

### Next

Milestone 3 — tools. A read-only text-to-SQL tool over the expense database, so
the two refused questions above become answerable. Numbers will come from a query,
never from the model.
