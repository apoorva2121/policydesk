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
