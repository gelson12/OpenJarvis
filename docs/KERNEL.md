# The OpenJarvis Kernel — the standard way OpenJarvis operates

> One deterministic contract for "can I do this, and did it work" — so the
> assistant is reliable, predictable, and never lies about its own abilities.

## Why this exists

Every failure in the 2026-05-29 voice session had the **same root cause**: the
LLM was allowed to decide two things it must never decide.

1. **Whether a capability exists.** The user asked for their Outlook calendar;
   the server *had already fetched it*, yet the model said *"I don't have the
   tools to access your Outlook calendar."*
2. **Whether an action succeeded.** Both booking providers threw DNS errors,
   and the user was told *"there are no houses to rent in Lisbon."* A total
   outage was reported as an empty market.

The codebase had tried to paper over this with layer upon layer of band-aids
that fought each other: `intent_preexec` injected data plus *"ABSOLUTE
INSTRUCTIONS: do not disavow"*, an *"ANTI-DISAVOW GUARD"*, *Round 5/6/7* hacks,
and a 141-tool LLM loop that stalled for minutes. **That patchwork was the
inconsistency.**

Crucially, the tools already knew the truth — `ToolResult.success` is `True`
for real data, `False` for an error — but the pre-exec layer threw that flag
away. So a token-expiry error, an empty calendar, and real events all looked
identical to the LLM, which then guessed.

## The contract

Every turn resolves to exactly one **Outcome**. Its `status` is the single
source of truth:

| status        | meaning                                              | who speaks |
|---------------|------------------------------------------------------|------------|
| `OK`          | real data; reply built deterministically             | kernel     |
| `EMPTY`       | ran fine, genuinely nothing (0 events, 0 listings)   | kernel     |
| `ERROR`       | capability exists but failed (auth/network) — honest | kernel     |
| `NEEDS_INPUT` | understood, a slot is missing ("which city?")        | kernel     |
| `HANDLED`     | (worker) capability already spoke/acted              | —          |
| `PASSTHROUGH` | no capability claims this turn → hand to the LLM      | LLM        |

**The LLM is never asked "can you do X?"** The registry answers that. The LLM
is reached only for `PASSTHROUGH`, and even then it is handed the capability
**manifest as ground truth**, so it still cannot disavow a real capability.

`ERROR` is never rendered as `EMPTY`. A failed calendar fetch says *"I couldn't
reach your Outlook just now, sir"* — never *"you have no meetings."*

## Two halves, one contract

The system is two deploy units (a Railway single container running both), so
the kernel has two halves that obey the identical contract:

### Server kernel — the brain (`src/openjarvis/kernel/`)
Owns **data capabilities** (calendar, email). Wired into
`routes.py::chat_completions` as the **first authority**: it executes the real
tool, honours `success`, and streams a finished answer in OpenAI SSE format —
the cloud LLM is bypassed entirely for these turns. No disavowal possible, and
no tool-loop to stall (this is what caused the multi-minute silences).

```
contracts.py           Outcome / OutcomeStatus / CapabilitySpec
calendar_capability.py  detect → fetch (success-preserving) → deterministic reply
email_capability.py     same discipline for the inbox
core.py                 resolve(text) → Outcome ; manifest() → LLM ground truth
sse.py                  Outcome → OpenAI SSE stream / ChatCompletionResponse
```

Kill-switch: `OPENJARVIS_KERNEL_ENABLED=false`.

### Worker kernel — the body (`livekit/kernel.py`)
Owns **device capabilities** (camera, gesture mode; widgets/desktop/etc. as
they migrate). A single ordered `WorkerKernel` registry replaces the pile of
`_maybe_handle_*` branches. Native capabilities are built on the contract;
legacy handlers plug in unchanged via `register_legacy()` and are rewritten
into native capabilities over time — **without another architectural change.**

## Adding a capability (the standard)

1. Write `detect(text)` (or reuse an intent regex) — pure, testable.
2. Execute the real tool/action. **Capture success/failure explicitly.**
3. Return an `Outcome`:
   - real data → `Outcome.ok(<spoken summary built from the data>)`
   - genuinely nothing → `Outcome.empty(<honest "nothing found">)`
   - it failed → `Outcome.error(<honest "couldn't reach X">)`
   - missing a slot → `Outcome.needs_input(<the question>)`
   - not your turn → `Outcome.passthrough()`
4. Register it (server: add to `core._CAPABILITIES`; worker:
   `kernel.register(...)`). Add it to the manifest if it's user-visible.
5. Write tests that prove **OK, EMPTY, and ERROR are three distinct outcomes**
   — especially that a failure never reads as empty.

## Rules

- **Never** let the LLM assert a capability is missing. If it exists, the
  registry knows, and the manifest tells the model.
- **Never** collapse `ERROR` into `EMPTY`. Honesty about failure is the whole
  point.
- The deterministic path **speaks before** any slow await (LiveKit voice
  silence is a bug) and produces a fixed, butler-tone reply — no LLM in the
  loop, so it's instant and consistent.
- A broken capability is skipped, never fatal — the turn always survives.

## Tests

```
PYTHONPATH=src     python -m pytest tests/test_kernel.py            # server: 14
PYTHONPATH=src     python -m pytest accommodation/tests/            # nlu+agg: 50
PYTHONPATH=livekit python -m pytest livekit/test_kernel.py          # worker: 24
```
