# Ex9 — Reflection

## Q1 — Planner handoff decision

### Your answer

In my Ex7 run (session `sess_950730a8d1c7`, 2026-05-09T16:25:43), the planner
was called with the task "book for party of 12 in Haymarket" and produced a
single subgoal with `assigned_half: "loop"`. The handoff to the structured half
wasn't a planner decision at all — it was the executor. Looking at the trace at
timestamp `2026-05-09T16:25:43.791208+00:00`, the executor called
`handoff_to_structured` with this reason:

> "loop half identified a candidate venue; passing to structured half for
> confirmation under policy rules"

The phrase "policy rules" in the context was the signal. The executor saw that
`handoff_to_structured` was available in the tool registry, the task mentioned
confirmation under rules, and it made the call. The planner never explicitly
assigned anything to the structured half in this session.

That gap matters. If you removed `handoff_to_structured` from the tool registry
(which I actually did briefly to stop the LLM spiralling in Ex5), the executor
would have no mechanism to escalate regardless of what the task said. The
architectural lesson is that the planner's `assigned_half` field is advisory —
it shapes which tools the executor is pointed at, but the actual transition
happens when the executor calls the tool. The two-level design (plan → execute →
call) gives flexibility but means you can't reason about half-assignment from
the planner output alone; you need to follow the executor trace.

### Citation

- `sess_950730a8d1c7/logs/trace.jsonl` — `executor.tool_called` event at
  `2026-05-09T16:25:43.791208+00:00`, tool: `handoff_to_structured`
- `sess_950730a8d1c7/session.json` — planner subgoal has `assigned_half: "loop"`
  throughout all three rounds

---

## Q2 — Dataflow integrity catch

### Your answer

In session `sess_ffc201998952` (a real-LLM run), the LLM spiralled through four
`venue_search` calls with party_size=50, 50, 50, and then 20 — all returning
zero results because no venue in the fixture seats 50 people. Rather than
failing gracefully, it fabricated a venue called "The Royal Edinburgh" and
called `generate_flyer` directly with made-up event details
(`{"venue_name": "The Royal Edinburgh", "address": "1 Castle Rd, Edinburgh",
"event_date": "2023-11-15", ...}`). It never called `get_weather` or
`calculate_cost`. The flyer had blank cost and weather fields.

The integrity check passed vacuously — no numeric facts in the flyer to
challenge. That's a problem, but the run itself fails because there's no
`complete_task` call and no flyer with actual data.

The more direct example of the check doing its job: in the offline scripted
session `sess_bca8d3033870`, the `FakeLLMClient` scripts `generate_flyer` with
`total_gbp=540` and `deposit_required_gbp=0`. The real `calculate_cost` tool
returns `{total_gbp: 556, deposit_required_gbp: 111}` and logs that to
`_TOOL_CALL_LOG`. The scripted £540 only passes `verify_dataflow` because the
`generate_flyer` tool itself logs its own `event_details` argument, putting 540
in the log. If I replay the check without logging the `generate_flyer` call —
simulating a case where the LLM injected £540 into the prompt rather than
getting it from `calculate_cost` — `verify_dataflow` returns
`ok=False, unverified_facts=['£540', '£0']`. The £9999 fabrication test from
the README confirms the same pattern: change the flyer manually and the check
reports `dataflow FAIL: 1 unverified fact(s): ['£9999']` because 9999 never
appeared in any tool output.

### Citation

- `sess_ffc201998952/logs/trace.jsonl` — four `venue_search` calls at
  12:30:27–12:30:42, then `generate_flyer` at 12:31:19 with fabricated venue
- `sess_bca8d3033870/workspace/flyer.html` — shows £540, £0 from scripted client

---

## Q3 — Removing one framework primitive

### Your answer

Shipping this agent to a real pub, the first production failure I'd expect is
the Rasa action server being temporarily unavailable — slow cold start, a
restart after a config change, or a transient network hiccup. My `sess_950730a8d1c7`
trace shows exactly this happening in development: every round returned
`"rejection_reason": "rasa unreachable: <urlopen error [Errno 61] Connection
refused>"` three times before the bridge hit `max_rounds_exceeded`. In
production that means the customer's booking attempt silently exhausts all retry
rounds and the session ends in a failed state with no actionable output.

The primitive that surfaces this cleanly is the **fail-closed IPC rule** — at
most one handoff file visible in `ipc/` at any time. When the bridge writes a
forward handoff and Rasa immediately errors, the bridge archives the file before
starting the next round. If the process crashes between writing and archiving,
the stale file is still there and the next poll can detect it as a stuck session.
The invariant — one file maximum — makes the failure mode observable. In
`sess_950730a8d1c7`, after the session exhausted three rounds, the `ipc/`
directory was left clean; the full rejection chain is in
`logs/trace.jsonl` as three pairs of `session.state_changed` events
(loop → structured, structured → loop) with the reason inline. Without the
archiving step that enforces the one-file rule, you'd accumulate three stale
handoff files and have no way to tell which round they belong to.

The fix in production would be to add a Rasa health check before each forward
handoff and surface the "service down" state as an explicit ticket error rather
than burning retry rounds on it.

### Citation

- `sess_950730a8d1c7/logs/trace.jsonl` — three `session.state_changed`
  (structured → loop) events each with `rejection_reason: "rasa unreachable:
  Connection refused"`
- `starter/handoff_bridge/bridge.py:147–151` — the archive step that enforces
  the one-file-in-ipc rule
