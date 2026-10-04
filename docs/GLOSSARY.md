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


## Retrieval and cost (Milestone 2)

**Chunk strategy** -- how a document is cut up. `section` splits at headings so one
chunk is one complete rule; `fixed` splits every N characters with overlap.

**Overlap** -- repeating the last few characters of one chunk at the start of the
next, so a sentence cut in half still appears whole somewhere.

**Contextual header** -- a short label (document, page, section) added to a chunk
*before embedding*, so a short chunk is distinctive. It is not shown to the user.

**Cosine distance / similarity** -- how we measure "close in meaning". Distance 0
means identical direction; we report similarity as 1 minus the distance.

**top_k** -- how many chunks retrieval returns. Too few and you miss the answer;
too many and you pay for irrelevant text and invite the model to be distracted.

**Manifest (index)** -- a small record of how an index was built: embedding model,
chunk strategy, sizes. Checked on load, because querying an index built with a
different embedding model returns confident nonsense without any error.

**Response cache** -- storing a model's reply on disk, keyed by a hash of the exact
request, so identical repeats are free and instant.

**Token pre-check** -- counting tokens locally before sending a request, so an
oversized prompt is refused instead of paid for.

**Refusal token** -- an exact string the model emits when the evidence is missing
(`INSUFFICIENT_EVIDENCE`), so our code can detect a refusal reliably.

**Grounded vs cited** -- grounded means the claim really is supported by the
retrieved text; cited means the answer points at a source. A system can cite
without being grounded, which is why Milestone 4 adds a verification step.

**Prompt injection** -- text inside a retrieved document that tries to give the
model instructions. Telling the model to ignore it helps but is **not** a security
boundary.


## Tools and SQL safety (Milestone 3)

**Tool** -- a function the model may ask for. The model chooses *when*; your code
controls *what is possible*. Every real protection is on your side.

**Tool allow-list / registry** -- the set of tools that exist in a run. A model
cannot call anything outside it. This is a control; telling the model which tools
exist is not.

**Input schema** -- a declared shape for a tool's input, validated before the tool
body runs, so a tool never sees input it did not ask for.

**Failure as data** -- returning `ok=False` with a reason instead of raising, so an
agent can see the failure and decide what to do next.

**Safe error vs internal detail** -- the user-facing reason, versus the raw cause
(paths, passwords, rejected SQL) which is logged and never shown.

**Text-to-SQL** -- turning a natural-language question into a database query. The
model writes the query; the database does the arithmetic.

**SQL authorizer** -- a callback SQLite invokes to approve each operation it is
about to perform, by type and table. It does not parse strings, so clever SQL
cannot evade it. Default-deny.

**Read-only connection** -- opening the database file `mode=ro`, so writes are
impossible at the operating-system level.

**Default-deny** -- refuse anything not explicitly permitted, rather than allowing
anything not explicitly forbidden. The only safe default.

**Allow-list vs deny-list** -- an allow-list names what is permitted (short, safe);
a deny-list names what is forbidden (always incomplete).

**Business glossary (semantic layer)** -- notes in the prompt mapping how people
speak ("travel spend", "T&E") to what the schema holds. Without it the model
invents filters that match nothing.

**Suspicious result** -- an empty or all-NULL result, reported as doubtful rather
than as an answer, because it is also exactly what a wrong filter looks like.

**CTE (common table expression)** -- a `WITH name AS (...)` block naming an
intermediate result inside one query. Not a table.

**Known-answer test** -- a test whose expected value you computed yourself,
independently of the code. The only kind that catches a subtle regression.


## Measurement, serving and operations (Milestones 7-10)

**Evaluation set / golden set** -- questions with known correct answers, kept in
version control. The only way to tell a real improvement from a lucky run.

**Ground truth** -- the correct answer, established by a person. An expected answer
produced by a model is not ground truth; it is the same guess with more confidence.

**Recall@k** -- of the passages you needed, what share did retrieval actually
return. Says nothing about whether the answer used them.

**MRR (mean reciprocal rank)** -- how high up the list the first useful passage
appeared. 1.0 means first. Catches the difference between "found it" and "found it
ninth", which recall cannot see.

**Churn (in a comparison)** -- how many cases changed verdict at all. The number to
check before believing a small delta: four flips each way nets to zero and means
unstable, not unchanged.

**Noise floor** -- the amount a score moves between runs of the *same*
configuration. A change smaller than the noise floor has not been demonstrated.

**Span / trace** -- a span is one timed step; a trace is the tree of spans for one
request. Logs give you a sequence; a trace gives you a shape and a duration.

**Liveness vs readiness** -- liveness answers "am I alive" (failing means restart
me); readiness answers "can I serve a request" (failing means stop sending me
traffic). Different questions with different responses.

**Structured logging in production** -- `LOG_JSON=true` emits one JSON object per
line, which is what log collectors parse. Humans get the colourful version in dev.

**Multi-stage Docker build** -- build in one image, copy only the result into a
smaller final image. Fewer packages in the thing you run means fewer to patch.

**Non-root container** -- running as an unprivileged user, so a compromise gets
that user's rights rather than root's.

**Volume** -- a directory kept outside the image. State belongs here; baking it in
means rebuilding the image to change data.

**Scales to zero** -- a hosting model that costs nothing when idle. Right for an
internal tool used in bursts.

**Row-level security** -- the database itself restricts which rows a given login
can see. Enforced by the database, not by a prompt or a regex.
