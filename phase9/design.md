# Phase 9 — Agent Workspace: evals + dashboard (best-practice unified design)

**Date:** 2026-04-28
**Driver question:** how should mur let users (a) test their agent configs against multiple LLM providers / system prompts / MCP / skills, and (b) manage agents in a web dashboard?

## Key insight (after deep-research of Promptfoo / Langfuse / LangSmith / Inspect AI / Dify / n8n / Phoenix)

The two items (test platform + agent dashboard) **are not two products**. Every mature LLM tooling stack converges on one architecture:

1. **Single central object** — an agent / workflow / app. All UI is about it.
2. **Trace store as foundation** — evals, playground, and prod messages all push into the same trace pipe.
3. **CLI is the API; web is a thin renderer** — power users + CI use CLI; the dashboard never re-implements actions.
4. **Versioning + portability** — system prompts, profiles, eval suites all serialise to a shippable bundle.

mur already has #1 (the agent profile + agent_home dir) and a partial #2 (`telemetry/<date>.jsonl` per agent), and #4 (`.murpkg` export). The missing pieces are: (a) eval suites + runner, (b) HTTP routes that surface agent state, (c) a web tab on top.

## Architecture

```
                    ┌─────────────────────────────────┐
                    │        mur agent CLI            │
                    │  create / send / list / export  │
                    └─────────────┬───────────────────┘
                                  │ JSON-RPC over UDS
                                  │
                    ┌─────────────▼─────────────┐
                    │     mur-agent-runtime     │  ← per-agent process
                    │  profile + sys_prompt +   │
                    │  mcp + skills + LLM       │
                    └─────┬─────────────┬───────┘
                          │             │
                          ▼             ▼
              ┌──────────────────┐  ┌──────────────────┐
              │ ~/.mur/agents/   │  │ ollama / antrhopic│
              │   <name>/        │  │ openai HTTP      │
              │     profile.yaml │  └──────────────────┘
              │     sys_prompt.md│
              │     telemetry/   │  ◄─── all agent activity (prod + eval)
              │     evals/       │  ◄─── new in P9
              └──────────────────┘
                   ▲      ▲
                   │      │
                   │      └────  mur eval (NEW)  ←──  agent.eval.yaml
                   │              runs the suite, writes evals/<id>.json
                   │
                   └─────  mur serve /api/v1/agents/* (NEW)
                          axum routes: list / detail / status / send
                                       / telemetry / evals / SSE stream
                          + SPA tab for agents (Phase 3)
```

`mur eval` and `mur serve`'s agent routes are independent. Either one alone is useful; together they form the "agent workspace".

## Eval suite schema (`agent.eval.yaml`)

Versioned alongside `.murpkg`. Shippable to any host that has `mur` + the providers configured.

```yaml
name: customer-support-eval-v1
agent: support_bot                  # name OR path to .murpkg

# Optional matrix. When omitted, uses the agent's own profile values.
matrix:
  providers:
    - ollama/llama3.2:3b
    - ollama/qwen3:4b
    - anthropic/claude-sonnet-4-6
  system_prompts:
    - "@sys_prompt.md"              # the agent's own
    - "@sys_prompt-v2.md"           # candidate revision
  # mcp + skills can also be matrixed; off by default

# Test cases. Each runs against every (provider × system_prompt) cell.
test_cases:
  - id: greet
    input: "hello"
    expects:
      - kind: contains
        any_of: [hello, hi, hey, welcome]
        case: insensitive
      - kind: not_contains
        any_of: [error, "sorry I can't"]

  - id: refund_classification
    input: "where is my invoice for January"
    expects:
      - kind: equals
        case: insensitive
        value: billing

  - id: tone_polite
    input: "you are useless"
    expects:
      - kind: llm_judge
        rubric: "Is the reply polite, de-escalating, and offers a next step?"
        judge: ollama/llama3.2:3b
        threshold: 0.7

  - id: shape_json
    input: "alice@x.com wants a quote for 50 units"
    expects:
      - kind: json_shape
        keys_required: [name, email, intent]

# Output preferences.
report:
  formats: [markdown, jsonl, junit]
  diff: side-by-side
  store_traces: true                  # write all attempts into agent_home/evals/
```

### Scoring kinds (cheap → expensive)

| kind | how it scores |
|------|---------------|
| `contains` / `not_contains` | substring match (case option) |
| `equals` | exact match (case option) |
| `regex` | regex match |
| `length_min` / `length_max` | char/word length bounds |
| `json_shape` | response parses as JSON, contains required keys |
| `llm_judge` | a cheap model scores the response against a rubric, threshold gates pass |
| `script` | user-supplied Python callable returns 0/1 (escape hatch) |

A test case may stack multiple expects — all must pass for the case to pass.

## Output: `~/.mur/agents/<name>/evals/<run-id>.json`

```json
{
  "run_id": "eval-2026-04-28T14:00:00-abc123",
  "suite": "customer-support-eval-v1",
  "agent": "support_bot",
  "started_at": "2026-04-28T14:00:00Z",
  "finished_at": "2026-04-28T14:03:21Z",
  "matrix": {
    "providers": ["ollama/llama3.2:3b", "ollama/qwen3:4b"],
    "system_prompts": ["sys_prompt.md"]
  },
  "results": [
    {"cell": {"provider": "ollama/llama3.2:3b", "system_prompt": "sys_prompt.md"},
     "test_id": "greet", "passed": true, "checks": [...], "latency_ms": 312, "input_tokens": 45, "output_tokens": 12},
    {"cell": {"provider": "ollama/qwen3:4b", ...}, "test_id": "greet", "passed": false, "failure": "..."},
    ...
  ],
  "summary": {
    "total": 8, "passed": 6, "failed": 2,
    "by_provider": {"ollama/llama3.2:3b": "4/4", "ollama/qwen3:4b": "2/4"}
  }
}
```

The dashboard's "evals" tab is just a renderer over these files.

## `mur serve` agent routes (Phase 3 sketch — not built today)

| Method | Path | Purpose | Auth |
|--------|------|---------|------|
| GET | `/api/v1/agents` | list all under `~/.mur/agents/` | readonly OK |
| GET | `/api/v1/agents/{name}` | full profile + status + recent telemetry | readonly OK |
| POST | `/api/v1/agents/{name}/messages` | proxy to UDS `message/send` | write |
| GET | `/api/v1/agents/{name}/telemetry?since=ts` | tail JSONL | readonly |
| WS | `/api/v1/agents/{name}/stream` | live telemetry tail | readonly |
| GET | `/api/v1/agents/{name}/evals` | list eval runs | readonly |
| GET | `/api/v1/agents/{name}/evals/{run_id}` | per-run detail (matrix + cells) | readonly |
| POST | `/api/v1/agents/{name}/evals/run` | kick off `mur eval` (queue + return run_id) | write |
| PUT | `/api/v1/agents/{name}/profile` | edit profile fields (sys_prompt, perm) | write |
| POST | `/api/v1/agents/{name}/spawn` | start runtime | write (admin) |
| POST | `/api/v1/agents/{name}/stop` | stop runtime | write (admin) |

Reuses existing axum + readonly + CORS infrastructure. SPA components live in `mur-web-dist` (existing pipeline).

## Phased delivery plan

| Phase | Scope | Where | Estimated effort |
|-------|-------|-------|-------------------|
| **0** | Design (this doc) | mur-agent-harness/phase9/ | done |
| **1** | Python `evals/` prototype: schema parser, runner, scorers (contains / equals / regex / shape / llm_judge), markdown + jsonl report | mur-agent-harness | 1 day |
| **2** | Use the prototype to actually iterate on real eval suites (5-10 cases minimum). Stabilise schema. | mur-agent-harness | 1 week of usage |
| **3** | Port to Rust as `mur eval` subcommand. Same YAML, output writes to `~/.mur/agents/<name>/evals/`. | mur-run/mur PR | 2-3 days |
| **4** | Add `/api/v1/agents/*` read-only routes to `mur serve`. List + detail + telemetry + evals. | mur-run/mur PR | 2 days |
| **5** | SPA `agents` tab in mur web dashboard | mur-run/mur (or mur-web-dist) | 3-5 days |
| **6** | Write actions: spawn / stop / send-message / edit-prompt / kick-eval. Auth + CSRF. | mur-run/mur PR | 1 week |

Each phase independently shippable. We do Phase 1 today.

## Why this is "best practice" for long-term UX

1. **One mental model**: an agent is the unit; everything you do (run, send, eval, edit, observe) is on an agent.
2. **No drift between CLI and web**: web uses CLI's outputs (eval JSON files, telemetry JSONL) — they can't disagree.
3. **CI-friendly out of the box**: `mur eval suite.yaml` returns non-zero on regression; junit format plugs into any CI.
4. **Portable**: a `.mureval` file (or in-tree `agent.eval.yaml`) ships to any host that has `mur` + provider keys. No vendor lock-in.
5. **Versioning is implicit**: every eval run gets a `run_id`; `system_prompt`s can be referenced by file path (and thus by git commit).
6. **Trace store grows with usage**: `~/.mur/agents/<n>/{telemetry,evals}/` IS the observability backend. No separate Langfuse to install.

## Today's slice

1. ✅ This design doc.
2. Python prototype: `evals/runner.py` reads YAML, executes matrix, writes report. Scorers: `contains`, `equals`, `regex`, `length`, `json_shape`, `llm_judge`. (No `script` — too much rope today.)
3. One real eval suite: `evals/examples/customer-support.eval.yaml` with 4-5 test cases over 2 providers (one echo for control, one real LLM).
4. Run it; commit results.
