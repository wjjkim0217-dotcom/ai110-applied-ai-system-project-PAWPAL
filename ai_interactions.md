# AI Interactions Log

This file covers the PawPal+ agent's saved reasoning traces (Agentic Workflow Enhancement),
how I used a coding agent to build the project (Reflection on AI Collaboration), and a
comparison of two system prompts on the same task (Fine-Tuning or Specialization Behavior).

---

## PawPal+ agent: saved reasoning traces

The PawPal+ agent ([pawpal_agent.py](pawpal_agent.py)) works in several steps. It retrieves
guidelines, then the model plans and calls tools (`add_pet`, `add_task`, `build_plan`,
`update_task`, ...). It reads what `build_plan` reports, fixes conflicts, and a deterministic
self-check runs at the end. Every step is written to `logs/pawpal_agent.log` with a run id.
That folder is git-ignored, so real traces are committed here:

| Committed trace | What it contains |
|---|---|
| [docs/evidence/agent_traces.log](docs/evidence/agent_traces.log) | Three full, unedited runs from the log: the two-pet request under the original and the revised system prompt, and a sick-cat request that ends in a vet referral |
| [docs/evidence/demo_run.txt](docs/evidence/demo_run.txt) | Output of `python -m evals.demo_run`: 4 end-to-end cases with a numbered trace, the AI's output, the schedule and PASS/FAIL checks |
| [evals/latest_results.json](evals/latest_results.json) | The structured log of the latest 6-scenario eval: every check, tool error and token count |

### Trace 1: plan, act, check, fix (run `18a62293`)

Request: *"I have a 2-year-old husky named Mochi and an adult cat named Luna. I work 9 to 5 on
weekdays. Mochi takes a pill at 08:00 and 20:00."* Model `gemini-3.5-flash-lite`, 2026-11-14.
Excerpt from [agent_traces.log](docs/evidence/agent_traces.log). Arguments are shortened with
`...`, and nothing else is changed.

```
retrieve: 5 guideline(s) for species ['cat', 'dog']: medication.md#medication-timing, cats.md#feeding, cats.md#senior-cats, dogs.md#bathroom-breaks, dogs.md#feeding
model: call 1: stop_reason=tool_use
tool: add_pet({"species": "dog", "name": "Mochi"}) -> {"ok": true, "pet": "Mochi"}
tool: add_pet({"name": "Luna", "species": "cat"}) -> {"ok": true, "pet": "Luna"}
tool: add_task({"category": "meds", "duration_minutes": 5, "sources": ["medication.md#medication-timing"], "fixed_time": "08:00", "title": "Morning Pill", ...) -> {"ok": true, ...}
  ... 7 more add_task calls (Mochi's evening pill, meals and bathroom breaks; Luna's meals) ...
model: call 2: stop_reason=tool_use
tool: build_plan({}) -> {"day": "Monday 2025-01-06", "scheduled": ["07:00-07:15 Mochi: Morning Bathroom Break (fixed time 07:00)", ...
model: call 3: stop_reason=tool_use
tool: update_task({"fixed_time": "08:25", "task_id": "05833cd6-..."}) -> {"ok": true, "task": "Luna: Morning Feeding, 10 min, medium priority, feeding, at 08:25, daily"}
tool: update_task({"task_id": "e9c17155-...", "fixed_time": "18:10"}) -> {"ok": true, "task": "Luna: Evening Feeding, 10 min, medium priority, feeding, at 18:10, daily"}
model: call 4: stop_reason=tool_use
tool: build_plan({}) -> {"day": "Monday 2025-01-06", "scheduled": ["07:00-07:15 Mochi: Morning Bathroom Break (fixed time 07:00)", ...
model: call 5: stop_reason=end_turn
check: round 0: 0 error(s)
run finished: status=proposed changes=10 issues=0 model_calls=5 tool_calls=14 tokens=17634/1033
```

**How the decision chain reads:**

1. **Plan from retrieved context.** The model got 5 guideline sections and created every pet and
   task in one turn. Each task cites a real section id, and `add_task` rejects ids that don't
   exist.
2. **Act through tools.** The model never writes times into a schedule itself. Tasks go through
   validated tools onto a draft copy of the owner's data.
3. **Observe.** `build_plan` runs the real Module 2 scheduler and reports that Luna's meals
   clash with Mochi's meals.
4. **Revise.** The model moved Luna's meals to 08:25 and 18:10 in one turn, then called
   `build_plan` again to confirm the fix.
5. **Verify.** The deterministic self-check (`check_plan()`) found 0 errors, so the draft went
   to the owner for Accept or Discard.

### Trace 2: recovering from its own bad tool calls (run `a404e9c9`)

The same request under the original system prompt. In its second turn the model sent 9
`add_task` calls without a `title`. Every one was rejected and returned to the model as an
error:

```
WARNING [a404e9c9] tool_error: add_task({"duration_minutes": 5, "pet_name": "Mochi", "notes": "Morning pill", "frequency": "daily", "priority": "high", ...) -> title must be 1-60 characters
  ... 8 more identical errors ...
INFO [a404e9c9] model: call 3: stop_reason=tool_use
INFO [a404e9c9] tool: add_task({..., "title": "Morning Pill", "priority": "high"...) -> {"ok": true, ...}
```

In call 3 it re-sent all 9 tasks with titles. It then needed four `build_plan` / `update_task`
rounds to clear the meal clashes, because it fixed them one or two at a time. That
inefficiency is what the prompt comparison below addresses.

### Trace 3: deciding not to act (run `0cc8b0bf`)

Request: *"My cat Nala has been vomiting since this morning and won't eat. Plan her care for
today."* Retrieval put `medication.md#when-to-call-a-vet` first. The model answered in a single
call with **no tool calls**: it told the owner to contact a vet and scheduled nothing, as the
system prompt requires.

---

## Using a coding agent to build PawPal+ AI

**What task did you give the agent?**

I used Claude Code for most of the project. I gave it the assignment's instructions directly
and asked for one step at a time: choose which earlier project to extend, design the AI
architecture, build the agent, retriever, tests and evals, run the real model, and write the
docs.

**What did the agent do?**

- Compared my earlier projects and recommended extending PawPal+, so the AI's plans would go
  through the scheduler I had already tested.
- Wrote `pawpal_agent.py`, `pawpal_rag.py`, the knowledge base, and offline tests with fake
  model clients, then ran pytest after each change.
- At my request, replaced the paid-only Claude setup with a free Gemini adapter
  (`pawpal_llm.py`), with Claude kept as an option.
- Ran the real eval scenarios, read the logs, and changed the system prompt to cut model calls
  (see the comparison below).

**What did you have to verify or fix manually?**

- Its first default model (`gemini-2.5-flash`) had been retired. Only a real run showed that.
- Its secret scan missed my API key in `.env.example`. GitHub's push protection blocked the
  push, and I removed the key before pushing.
- One README eval table had the wrong number of checks per scenario. Re-reading the raw JSON
  log caught it.
- I kept the work on `main` instead of the branch it suggested, and had it pause a bigger docs
  round until the earlier work was committed.

---

## Prompt comparison: original vs. revised system prompt

Same request (the husky and cat workday above), same model, same knowledge base. The only
difference is one paragraph added to `SYSTEM_PROMPT` in [pawpal_agent.py](pawpal_agent.py),
telling the model to batch tool calls, give every `add_task` all its required fields, fix all
conflicts in one turn, and only search for more guidelines when the provided ones don't cover
something.

| | Option A: original prompt | Option B: revised prompt |
|---|---|---|
| **Model / tool used** | `gemini-3.5-flash-lite` (free tier), run `a404e9c9` | `gemini-3.5-flash-lite` (free tier), runs `18a62293`, `f43430bd`, `2739db5a` |
| **Prompt** | Rules for tools, citations, medication and vet referral, then "call build_plan and fix any conflicts" | The same, plus the batching paragraph quoted above |
| **Response summary** | 2 pets, 9 tasks. Mochi got two **45-minute walks** and Luna a play session | 2 pets, 8-9 tasks. Mochi got two or three **10-15 minute "bathroom breaks"** as his walks |
| **What was useful** | Searched for `husky exercise walk` itself and followed the walks guideline | **5 model calls instead of 11**, 13-15 tool calls instead of 32, **0 tool errors instead of 9**, about 17-18k input tokens instead of 60k, 2 `build_plan` rounds instead of 4 |
| **Problems noticed** | 9 invalid calls, and conflicts fixed one at a time. 11 requests for one plan is costly on a free tier that allows only a few requests a day | Worse care: 20-45 walk minutes a day instead of 90. It stopped searching, and the 5 sections retrieved up front didn't include `dogs.md#walks-and-exercise` |
| **Decision** | Rejected | **Kept**, with the walk regression recorded as a known limitation |

**Which approach did you use in your final implementation and why?**

Option B. It is the only one that fits the free tier: about a third of the requests and tokens,
and no wasted invalid calls. Both of its scored runs (`f43430bd` in the eval, `2739db5a` in
the demo) passed every check. The comparison
also showed that efficiency cost quality. Telling the model to search less meant it planned
only from the first 5 retrieved sections, and for this request those sections were about
bathroom breaks, not exercise. My checks ("at least 2 walk tasks") did not catch that. The fix
I would make next is a stricter check (minimum walk minutes for high-energy breeds) plus
always retrieving the walks section for dogs. Measured numbers are summarized in
[model_card.md](model_card.md#baseline-vs-specialized-prompt).
