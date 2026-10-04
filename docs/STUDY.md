# How to study this project

You now have a working agentic RAG system and no understanding of it. That is a
normal and temporary situation, and this file is the way out.

The plan below is built around one principle: **you cannot learn this by reading
it.** You learn it by running something, predicting what will happen, being wrong,
and finding out why. Every session below is structured that way.

Budget: about **eight hours**, in sessions of 45–60 minutes. Model cost for all of
it: **under $1**, because the response cache makes re-runs free.

---

## Before you start: two rules

**Rule 1 — predict before you run.** Every command below comes with a question.
Write your answer down (one line, on paper or in a file) *before* you press enter.
Being wrong in writing is how this sticks; being wrong in your head is how it does
not. This is the single highest-value habit in the whole plan.

**Rule 2 — the bugs are the curriculum.** `docs/PROGRESS.md` records every bug found
while building this, with why it happened. There are eleven. They are worth more
than the working code, because they are what an interviewer is actually probing
when they ask "what went wrong?".

---

## Session 1 — See it work, and see it refuse (45 min)

No code reading. Just use it, and build intuition for what it can and cannot do.

```powershell
.venv\Scripts\python.exe -m policydesk.cli ui
```

Click each example button in the sidebar, in order. For each one, before you click:

| Question | Predict first |
| --- | --- |
| Policy lookup | Which document and page will it cite? |
| A number from the database | Will the number come from the model or a query? How will you know? |
| Both sources at once | How many tool calls? Will it get every breach? |
| Should be refused | What exactly will it say? |
| An attack | What happens to the poisoned document? |
| Needs human approval | What stops it writing immediately? |

For every answer, open **all four** expanders: sources, queries, what it did, where
the time went. Do not skip the queries one — reading the SQL is the habit that
separates people who trust these systems blindly from people who can operate them.

**Finish by answering out loud, in full sentences:**
1. Why did the cross-source question need two tools and not one?
2. What happened to the "USD 5000" in the vendor addendum, and at which step?

---

## Session 2 — Follow one question through the code (60 min)

Now read, but in execution order rather than file order.

```powershell
.venv\Scripts\python.exe -m policydesk.cli agent "What is the hotel limit in Tokyo?" --trace
```

The trace names the nodes it visited. Open each one in this order and read only the
function named in the trace:

1. `graph/state.py` — read the module docstring. **What is in the state object, and
   why is `attempts` there?** This is a direct interview question.
2. `graph/nodes.py` → `guard_input_node`
3. `graph/nodes.py` → `plan_node`, then `_parse_steps`
4. `graph/nodes.py` → `gather_node` — find the check that refuses writing tools
5. `graph/nodes.py` → `grade_node`
6. `graph/nodes.py` → `compose_node` — read the system prompt properly
7. `graph/nodes.py` → `verify_node` — **this one makes no model call. Why?**
8. `graph/build.py` — read `route_after_grade` and its docstring

**Then draw the graph from memory**, on paper, including both loops and the
interrupt. Check it against `policydesk diagram`. Redraw whatever you got wrong.

---

## Session 3 — Break it on purpose (60 min)

Nothing teaches a design like watching it defend itself. Run each of these and
predict the outcome first.

```powershell
# 1. Can you make it run forever?
#    Edit .env: MAX_GRAPH_LOOPS=0, then ask a cross-source question.
#    PREDICT: how many search attempts, and what does it answer?

# 2. Can you make it spend money?
#    Edit .env: MAX_SPEND_USD_PER_RUN=0.0001, then run the agent.
#    PREDICT: what happens, and at which call?

# 3. Can you make it query a table it should not?
.venv\Scripts\python.exe -m pytest tests/test_sql_safety.py -v
#    Read the test NAMES. Each is an attack. Pick three and explain why each fails.

# 4. Can you poison it?
#    Open data/raw/policies/vendor-addendum-UNTRUSTED.pdf and read it.
#    Then: policydesk agent "what is the meal per-diem limit?" --trace
#    PREDICT: which step stops it, and what does the trace say?

# 5. Can you make it write without approval?
.venv\Scripts\python.exe -m pytest tests/test_graph.py -k "refuses_to_run_a_tool_that_writes" -v
#    Read that test and its docstring. This was a REAL bug. Why did the approval
#    gate not catch it?

# 6. Can you make the index lie?
#    Edit .env: EMBEDDING_MODEL=text-embedding-3-large, then: policydesk ask "anything"
#    PREDICT: does it crash, return nonsense, or refuse?
```

Put every setting back afterwards. Question 5 is the most important one in this
session — the answer, "a gate protects one path; I had not enumerated every path",
is a strong thing to be able to say.

---

## Session 4 — Measure something (60 min)

This is the session that separates you from someone who has only built a demo.

```powershell
# Establish where you are
.venv\Scripts\python.exe -m policydesk.cli eval --label mine --save

# Read the dataset and understand what is being checked
.venv\Scripts\python.exe -m policydesk.cli eval-cases
```

Read `evaluation/dataset.py` properly, especially the `note` on `cross-01` and the
long one on `adv-02`. Then:

```powershell
# Change ONE thing. Edit .env: CHUNK_STRATEGY=fixed
.venv\Scripts\python.exe -m policydesk.cli index          # the index MUST be rebuilt
.venv\Scripts\python.exe -m policydesk.cli eval --label fixed-chunks --save
.venv\Scripts\python.exe -m policydesk.cli eval-compare
```

**Predict before you run it: will fixed chunking be better, worse, or the same?**
Then read the comparison and answer:

1. What is the delta, and what is the churn?
2. Is this a real difference or noise? How do you know?
3. Which *category* moved, and does that point at retrieval or generation?

Then try one more change of your own: `RETRIEVAL_TOP_K=10`, or
`CHUNK_SIZE_CHARS=400`. Same discipline: predict, run, compare, explain.

**The headline finding to internalise:** gpt-4o scored identically to gpt-4o-mini
at 4.5× the cost. If you remember one thing from this project, make it that
"use a better model" is a hypothesis, not a plan.

---

## Session 5 — Improve the weak spot (60 min)

Cross-source questions pass 1 to 2 of 4, and `cross-01` and `cross-03` fail in
every run. That is the open problem, and now you have a way to tell whether you
have actually fixed it.

Pick **one** hypothesis, change **one** thing, measure:

| Hypothesis | Change to try |
| --- | --- |
| The composer is drowning in evidence | Cap the evidence passed to `compose_node` at the top 8 pieces |
| The composer needs the limits and rows separated | Group evidence by kind (POLICY first, then DATA) in `Evidence.for_prompt` |
| The planner's SQL misses fields the rules need | Tighten the field list in `PLANNER_SYSTEM` |
| Retrieval ranks the limits table too low | Raise `top_k`, or add a keyword search alongside the vector one |

Run `eval --label <your-hypothesis> --save` then `eval-compare`. **Write down what
you predicted and what happened, even — especially — when nothing improved.** A
measured null result is a real finding, and being able to describe one is more
convincing in an interview than a success story with no measurement behind it.

---

## Session 6 — Rehearse the interview answers (45 min)

For each question, answer **out loud**, in four to six sentences, in this shape:

> **define or confirm → why it happens → the cheapest fix first → one trade-off →
> one concrete example from this project**

That shape is the thing to drill. Your last mock scored 2.75/5 because answers were
keyword lists with no "why" and no example. You now have examples for all of these.

**RAG mechanics**
1. Chunk size and overlap — and why? *(the meal rule holding both USD 60 and USD 85)*
2. How do you cite at page level? *(the location travels from extraction onward)*
3. What happens if you change the embedding model? *(the manifest refuses to load)*
4. How do you tell a retrieval bug from a generation bug? *(separate metrics: recall
   0.92 and cross-source 1/4 point at generation)*

**Agents**
5. What is in your state object? *(and why `attempts` is a number in code, not an
   instruction in a prompt)*
6. How do you stop an agent looping forever? *(a counter checked in an edge)*
7. LangGraph versus a plain chain? *(cycles, conditional routing, durable interrupts)*
8. How do you do human-in-the-loop properly? *(interrupt BEFORE the write, state on
   disk, and the node checks approval itself because routing can be wrong)*

**Safety**
9. How do you make text-to-SQL safe? *(four layers, and be clear that only the
   authorizer and the read-only connection actually hold)*
10. How do you stop prompt injection? *(the hierarchy — start with "the model cannot
    do the dangerous thing", not with "I told it not to")*
11. A document says "ignore your instructions". What happens? *(quarantined at the
    tool, reported, and the real answer still returned)*

**Measurement**
12. How do you know your agent works? *(a hand-derived eval set, separate metrics,
    churn reported alongside the delta)*
13. How would you halve this pipeline's cost? *(cache, cheaper model per step, fewer
    chunks, shorter outputs — output tokens cost 4× input)*
14. Tell me about a bug you found. *(pick from the eleven in PROGRESS.md — the
    planner reaching the write tool is the best one)*

---

## Session 7 — The production conversation (45 min)

Read `docs/DEPLOYMENT.md` and `docs/AZURE.md`.

Then answer, without looking:

1. This works. Why would you *refuse* to deploy it? *(start with authentication)*
2. How do you enforce document permissions? *(at retrieval time, in the query,
   against an authenticated user — never in the prompt)*
3. Design this on Azure. *(draw it: Entra ID, AI Search, Azure SQL read-only login,
   Container Apps, Key Vault, Service Bus for the review queue)*
4. What costs money when nobody is using it? *(AI Search bills per hour whether you
   query it or not; Container Apps scales to zero)*
5. How do you roll back a bad prompt? *(prompts are in Python files, so: git —
   which is *why* they are there and not in a database)*

---

## How to tell it worked

You have learned this when you can, from memory and without notes:

- [ ] Draw the graph, both loops, and the interrupt
- [ ] Name what is in the state object and why each accumulating field needs a reducer
- [ ] Explain why `verify_node` deliberately makes no model call
- [ ] List the four SQL safety layers and say which two actually hold
- [ ] Give the honest hierarchy of injection defences, strongest first
- [ ] Quote your own numbers: 86% overall, 1/4 cross-source, $0.017 a run
- [ ] Tell the gpt-4o story, including the cost ratio
- [ ] Tell three bug stories with the mechanism, not just the symptom
- [ ] Say what you would refuse to deploy, and why

## One honest warning

In an interview, this is **"a system I built to learn agentic RAG end to end"** —
never production or client work. That framing is a strength at your level: it reads
as deliberate upskilling, and it keeps you safe when somebody probes scale, users or
uptime. Claiming more than you did is the one thing that turns a good project into a
bad interview.
