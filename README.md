# mur-agent-harness

Multi-scenario test harness for [mur agent](https://github.com/mur-run/mur). Spawns fleets of agents, exercises the full RPC + multi-agent + cross-network surface, runs a heartbeat that restarts dead agents, and accumulates errors before triggering fix cycles.

Built during a "harness engineering" session that surfaced 5 mur-side bugs, all fixed in [PR #32](https://github.com/mur-run/mur/pull/32) (E2 provider parsing, E3 skill id aliases, E4 LLM backend wiring, E6 system-prompt forwarding, plus Anthropic + OpenAI client implementations).

## Layout

```
harness.py            Main orchestrator (~800 LOC)
a2a_send.py           Tiny JSON-RPC dialer for hosts without `mur` CLI
phase2/
  design.md           Scenario design + agent roster
  REPORT.md           Per-scenario outcomes + every error found and fixed
files/                Sample scenario artifacts (LLM outputs, tickets, outboxes)
run.md                Phase 1 single-agent feature-coverage notes
```

## Two ways to use it

### A. Pure-bash runbook (`bash_demo.sh`)
**Use this if you want to see "what mur agent feels like as a Unix tool"**, or to convince yourself the Python harness isn't doing anything magical.

```bash
./bash_demo.sh
```

End-to-end pipeline of real shell commands — `mur agent create`, `mur agent export`, `tar`, `scp`, `ssh`, `sed` to rewrite the profile name, `nohup` to spawn on the remote, `nc -U` to dial the agent's Unix socket and exchange JSON-RPC, `jq` to parse the reply. No Python anywhere. Same building blocks any sysadmin already has.

### B. Multi-scenario harness (`harness.py`)
**Use this for orchestration patterns** — parallel fan-out, heartbeat-driven recovery, HTTP webhook ingress, multi-turn dialog history, DAG dependency graphs. Bash gets unwieldy past ~3 simultaneous agents; Python's `threading` + `http.server` + `subprocess` is the right tool above that bar.

```bash
# all default scenarios
python3 harness.py --scenario all

# one scenario
python3 harness.py --scenario OFFICE-7-LLM

# include the env-gated remote-LLM scenario (needs healthy chat model on remote)
HARNESS_REMOTE_LLM=1 python3 harness.py --scenario all
```

`harness.py` looks for the patched mur binary at `/tmp/mur-fix-v241/target/release/mur` (the worktree built from PR #32). Adjust `MUR` at the top of the file if you keep the binary elsewhere.

## Scenarios

| ID | What it exercises |
|----|-------------------|
| OFFICE-1 | Single-agent Q&A + `tasks/list` |
| OFFICE-2 | Serial pipeline + filesystem-mediated file passing |
| OFFICE-3 | Fan-out triage 1 → 3 specialists |
| OFFICE-4 | `accepts_from` policy + CLI-as-trusted path |
| OFFICE-5 | Heartbeat detects SIGKILLed agent and restarts it (~4.5s) |
| OFFICE-6 | 50-message concurrent backpressure (~91 msg/s) |
| OFFICE-7-LLM | Real LLM (`llama3.2:3b` via Ollama): researcher (3 bullets) → summarizer (1 sentence) chain with files persisted to shared FS |
| OFFICE-8 | HTTP webhook on :7878 → intake → fan-out (quoter / docs / cs in parallel) → fan-in dispatcher → outbox file |
| OFFICE-9-XNET | `mur agent export` → `scp` to remote host → `tar xf` into `~/.mur/agents/` → symlink runtime → 3 round-trips via `ssh ... python3 a2a_send.py ...` |
| OFFICE-10-HYBRID | Local intake fan-out where one specialist runs on remote host; both legs return concurrently |
| OFFICE-11-XNET-LLM | (env-gated) cross-network real-LLM specialist; deferred when `qwen3:4b` on the remote is unresponsive |

## Cross-network setup

OFFICE-9 / OFFICE-10 expect:
- `HARNESS_REMOTE_HOST` env (default `karajan@commander.twdd.tw`) reachable via passwordless SSH (`-o BatchMode=yes`)
- `mur-agent-runtime` installed at `~/.local/bin/mur-agent-runtime` on the remote (run `cargo build --release -p mur-agent-runtime` from the mur source tree on a Linux host)
- `python3` on the remote (uses stdlib only)

## Heartbeat / recovery

- 3-second tick polling each agent via `agent/card`
- 2 consecutive failures → declare DEAD → kill PID if still up → respawn via the `mur_agent_<name>` symlink
- After-event `state.json` checkpoint so a fresh session can resume from the last completed phase

## License

Same as upstream mur (MIT).
