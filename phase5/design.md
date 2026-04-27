# Phase 5 — Replacing n8n / Dify with mur agent

**Goal:** show that the same patterns people reach for n8n or Dify can be expressed with mur agents + this harness as the orchestrator.

## n8n / Dify in 30 seconds

- **n8n** — visual workflow automation. Triggers (webhook, cron, polling), nodes (HTTP, transform, branch), and integrations (Slack, Sheets, GitHub, …). The canvas IS the program. Strength: integrations + non-coder UX. Weakness: opaque debugging, hard to version, hard to scale beyond a single instance.
- **Dify** — LLMOps. Knowledge bases (RAG), agent chains, prompt templates, conversational UIs over docs. Strength: turnkey RAG + chat UI. Weakness: agent runtime is a single hosted process, less control over isolation, telemetry, and policy.

mur agent's positioning is different: each agent is a long-running process with an explicit profile (model, system prompt, FS allowlist, network entitlements, MCP servers), addressed via JSON-RPC over a Unix socket. The orchestrator (this harness) owns the workflow shape. So we replace n8n's canvas with **Python harness code**, and replace Dify's hosted agent with **mur agents we control end-to-end**.

## Mapping table

| Scenario | n8n / Dify pattern | mur replacement | LLM? |
|----------|-------------------|-----------------|------|
| OFFICE-12 | n8n: Form → Webhook → Set → CRM node | HTTP POST → extractor agent (LLM) → JSON outbox file | yes |
| OFFICE-13 | n8n: Cron → multi-step report | Python loop fires periodic ticks → 3-agent chain → markdown report | echo |
| OFFICE-14 | Dify: Knowledge base RAG / Doc Q&A | Librarian agent with `fs_read` over `docs/` + system prompt requiring citations → LLM answers grounded | yes |
| OFFICE-15 | Dify: Planner-executor agent | planner agent decomposes a request into steps → executor runs each → aggregator summarises | echo |
| OFFICE-16 | n8n: Email → IF (category) → templated reply | Classifier agent (LLM) tags category → router selects template responder → outbox draft | yes (classify) |
| OFFICE-17 | Dify: Translation chain | Detector → translator → reviewer (3-stage LLM chain), each with its own system prompt | yes |
| OFFICE-18 | n8n: Log watcher → alert pipeline | File-watcher polls a log dir → analyzer agent classifies severity → alerter writes outbox | echo |

## Why these seven

They cover the four families of automation people actually buy n8n/Dify for:

1. **Event-driven ingress** (OFFICE-12, OFFICE-16) — webhook + classification + dispatch.
2. **Scheduled / polling** (OFFICE-13, OFFICE-18) — cron-style jobs + monitoring.
3. **Knowledge / retrieval** (OFFICE-14) — Dify's signature pattern.
4. **Multi-step LLM chains** (OFFICE-15, OFFICE-17) — agent-of-agents.

What's intentionally NOT here (and why):
- **Cross-network agents** — already validated in OFFICE-9 / OFFICE-10.
- **Agent-to-agent autonomous calls** — mur 2.4.1 has no tool layer for this; the harness orchestrates.
- **SaaS integrations (Slack post, Sheet append, etc.)** — those are HTTP wrappers; the n8n value-add is convenience, not capability. We simulate via files on shared FS.

## Output discipline

Each scenario writes its result to `files/<scenario>-<n>.{md,json}` so the artifact is inspectable after the run, like an n8n run history. The harness's `run.log` plus per-scenario outbox files together give the same auditability that n8n's execution history provides.

## What "passes" means here

- Echo scenarios: deterministic shape match — used to validate orchestration plumbing (fan-out, fan-in, FS, scheduling).
- LLM scenarios: permissive shape + content checks (e.g. "summary is non-empty + does not start with `echo:`"). Small models drift; we don't pretend otherwise.
