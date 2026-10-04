# Glossary

Plain-language definitions. A term is added here the first time it appears in
the project, so you never meet an unexplained word twice.

## Core AI ideas

**LLM (Large Language Model)** — the AI text model, for example GPT-4o. It
predicts text. It cannot see your files unless you put them in the prompt.

**Prompt** — the text we send to the model: instructions, the user's question,
and any documents we retrieved.

**Token** — a chunk of text, roughly 4 characters. Models are billed per token
and can only accept so many at once. That limit is the **context window**.

**RAG (Retrieval Augmented Generation)** — search your own documents first, paste
the best pieces into the prompt, then let the model answer from them. It fixes
two problems at once: the model does not know your data, and you want sources.

**Agentic RAG** — RAG where the model also decides *how* to search: which source,
how to rephrase, whether the result is good enough, whether to retry.

**Agent** — a loop where a model decides what to do next, possibly by calling
tools, until it reaches an answer or hits a limit you set.

**Tool** — an ordinary function the model is allowed to call, with a defined
input and output. Example: "run this SQL query". The model chooses *when* to call
it; your code decides *what it is allowed to do*.

**Grounded** — an answer is grounded when every claim in it is supported by the
retrieved text. Ungrounded means invented, also called a **hallucination**.

**Citation** — a pointer from a sentence in the answer back to the exact source
it came from (document, page, sheet, row).

**Chunk** — documents are too big to send whole, so we cut them into pieces and
search over the pieces.

**Embedding** — a list of numbers representing the meaning of a piece of text.
Similar meanings produce similar numbers, which is how we search by meaning
rather than by exact words.

**Vector store** — a database that holds embeddings and finds the closest ones
fast. We use Chroma locally.

**Reranking** — a second, slower, more accurate scoring pass over the first
search results, to push the best ones to the top.

## Engineering ideas

**Environment variable** — a setting that lives outside your code, in your shell
or a `.env` file. Used for anything that differs between machines, and anything
secret.

**`.env` file** — a local text file of settings. It is git-ignored, so secrets
never reach GitHub. `.env.example` is the committed template, with blank values.

**Fail fast** — check settings at startup and refuse to start with a clear
message, instead of crashing later somewhere confusing.

**Redaction** — replacing a secret with `***REDACTED***` before it reaches a log.

**Structured logging** — logging key/value pairs (`count=5 doc="policy.pdf"`)
instead of sentences, so logs can be filtered and searched by machine.

**Editable install** (`pip install -e .`) — installs your project so Python can
import it, but keeps reading the source from your folder, so your edits take
effect with no reinstall.

**Pinned version** — writing `pydantic==2.13.5` instead of `pydantic`, so code
that works today still works next month.

**Linter / formatter** — a linter finds likely mistakes; a formatter rewrites
layout into one consistent style. `ruff` does both for us.

**Fixture (pytest)** — reusable setup for tests. `autouse=True` applies it to
every test automatically.

**Idempotent** — running it twice has the same effect as running it once. Useful
for data pipelines, so a retry cannot create duplicates.

## Document extraction (Milestone 1)

**Text layer** -- the real characters stored inside a PDF. A normal PDF has one;
a scanned PDF does not, so extraction returns nothing at all.

**OCR (Optical Character Recognition)** -- reading characters out of a picture of
text. Needed for scanned documents. It is a separate step with its own error
rate, so OCR output should be treated as less trustworthy than a real text layer.

**Block** -- in this project, one citable piece of extracted content: a page of
PDF text, one PDF table, or one spreadsheet row. Blocks are bigger than a
sentence and smaller than a document.

**Locator** -- the short location string inside a source, such as `p.3` or
`October 2026!row 7`.

**Citation** -- the full pointer shown to a user: `travel-policy.pdf (p.3)`.

**Header detection** -- working out which spreadsheet row holds the column names,
instead of assuming it is row 1. Real spreadsheets have titles and notes above
the table.

**Cached formula value** -- the last result Excel calculated for a formula cell.
A file written by a script has none, so the cell reads as *empty*, not as zero.
Mistaking that for zero produces a confident wrong answer.

**Deterministic** -- same input, same output, every time. Our sample data is
deterministic on purpose: an evaluation score you cannot reproduce is not a
measurement.
