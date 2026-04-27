# Mur Agent Harness Phase 2 — Office Multi-Agent Design

**Version:** 2026-04-28
**Backend reality:** mur 2.4.1 runtime is hardcoded to `StubEcho` (supervisor.rs:125). Echo deterministically returns `echo: <input>`. We design scenarios that exercise *plumbing* not *content*.

## What's actually testable
- Multiple agents starting concurrently (UDS + lock files)
- `message/send`, `tasks/get`, `tasks/list`, `tasks/cancel`, `agent/card` JSON-RPC
- `accepts_from` / `sends_to` policy
- File transfer via shared filesystem (entitlements.filesystem.{read,write})
- Process supervision: spawn, kill, restart
- Heartbeat detection of dead agent → recovery

## What's NOT testable in 2.4.1 without code change
- Real LLM responses (fixed by E4 patch — written but not run; needs an LLM key/local model)
- File transfer JSON-RPC (no method handler; would need new RPC)
- Autonomous agent → agent calls (no LLM tool layer)

## Agent Roster

| Agent | Role | Provider | accepts_from | sends_to | FS read | FS write |
|-------|------|----------|--------------|----------|---------|----------|
| `concierge` | Office-1 single Q&A | ollama (stub) | * | [] | [] | [] |
| `intake` | Office-2 producer | ollama | * | [`dispatcher`] | [] | [`/tmp/mur-agent-harness/files`] |
| `dispatcher` | Office-2 consumer | ollama | [`intake`,`*-cli*`] | [] | [`/tmp/mur-agent-harness/files`] | [] |
| `triage` | Office-3 router | ollama | * | [`legal`,`it`,`hr`] | [] | [] |
| `legal`,`it`,`hr` | Office-3 specialists | ollama | [`triage`,`*-cli*`] | [] | [] | [] |
| `restricted` | Office-4 policy target | ollama | [`concierge`] | [] | [] | [] |
| `flaky` | Office-5 heartbeat target | ollama | * | [] | [] | [] |
| `bursty` | Office-6 backpressure target | ollama | * | [] | [] | [] |

10 agents total. Symlink directory: `~/.local/bin/`. Profile dir: `~/.mur/agents/`.

## Scenarios

### OFFICE-1 — Single-agent Q&A
- Boot `concierge`. Send 5 distinct messages with `tasks/list` polling between each. Confirm each task transitions to Completed and is retrievable via `tasks/get`.
- Pass: 5/5 completed, tasks list shows all.

### OFFICE-2 — Serial pipeline + FS file passing
- Boot `intake` and `dispatcher`. Harness sends to `intake`. Intake's response is captured by harness; harness writes a `ticket-N.md` file to `/tmp/mur-agent-harness/files/` (since echo agent can't write itself, we simulate the side-effect from harness side). Then harness sends to `dispatcher` referencing the file path. Dispatcher's echo confirms acknowledgement.
- Pass: file exists, dispatcher round-trip OK.

### OFFICE-3 — Fan-out triage 1 → 3
- Boot `triage`, `legal`, `it`, `hr`. Harness sends 6 messages with category keywords to `triage`. Harness reads triage's echo and routes to the matching specialist. Verify each specialist receives exactly 2 messages.
- Pass: 6 specialist tasks completed across the 3, distribution matches expectations.

### OFFICE-4 — accepts_from policy
- Boot `concierge` and `restricted`. Send to `restricted` directly via CLI (caller_pid resolution returns None → trusted user → allowed per spec line 25-38 of communication_policy.rs). Verify reaches restricted.
- Then simulate caller_pid = concierge's PID by inspecting communication_policy logic. Note: actual A2A inter-agent dial requires runtime-side dialler not available in 2.4.1 — so this scenario reduces to verifying CLI-as-trusted is allowed, and inspecting that the runtime's policy code path is exercised in a unit-style read.
- Pass: CLI message reaches `restricted`. Note documentation that full peer-to-peer test requires future runtime work.

### OFFICE-5 — Heartbeat + restart recovery
- Boot `flaky`. Harness pings every 3s with a `__hb__` message. After 3 pings, harness `kill -9` the runtime PID. Within 1 ping cycle (~3s), harness detects (timeout / connect error). Harness restarts `flaky` via `~/.local/bin/mur_agent_flaky &`. Resumes pinging. Verify continuation.
- Pass: detection latency < 6s, restart succeeds, post-restart pings all OK.

### OFFICE-6 — Concurrent backpressure
- Boot `bursty`. Harness fires 50 messages rapid-fire (no wait between). Polls `tasks/list` until all 50 are Completed. Measure throughput.
- Pass: 50/50 completed within 30s, no tasks stuck in Working.

## Heartbeat / Recovery Architecture

Single Python orchestrator `harness.py`:
- Reads `agents.yaml` roster
- Spawns each via the `mur_agent_<name>` symlink
- Maintains `/tmp/mur-agent-harness/state.json` checkpoint after every action
- Background heartbeat thread: every 3s, dial each agent's UDS with `agent/card` request (cheap). On 2 consecutive failures → mark DEAD → kill PID if still up → respawn → wait for socket → mark ALIVE.
- For LLM rate-limit (future): wraps each `message/send` call with exponential backoff on the `rate limit` error string, max 5 retries.
- Logs every action to `/tmp/mur-agent-harness/run.log` with timestamps.
- On harness exit (SIGINT/end), stop all agents cleanly.

## Error budget

Same rule as Phase 1 — accumulate 3 hard errors → enter fix mode. E4 (LLM wiring) is already known and patched as code (not run); doesn't count toward the budget unless we discover it broke.
