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
