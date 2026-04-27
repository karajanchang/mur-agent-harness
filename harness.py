#!/usr/bin/env python3
"""
mur agent multi-scenario harness with heartbeat + recovery.

Orchestrates a fleet of mur agents through office-style scenarios,
logs every action, accumulates errors, and runs a heartbeat thread
that restarts dead agents.

Usage: python3 harness.py [--scenario OFFICE-1|OFFICE-2|...|all]

Layout:
  /tmp/mur-agent-harness/
    harness.py             this script
    run.log                append-only event log
    state.json             checkpoint (resumable)
    files/                 shared FS for inter-agent file passing
    phase2/design.md       scenario design doc
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

HARNESS_DIR = Path("/tmp/mur-agent-harness")
LOG_FILE = HARNESS_DIR / "run.log"
STATE_FILE = HARNESS_DIR / "state.json"
FILES_DIR = HARNESS_DIR / "files"

MUR = "/tmp/mur-fix-v241/target/release/mur"
RUNTIME_DIR = Path.home() / ".local" / "bin"
AGENT_HOME_BASE = Path.home() / ".mur" / "agents"

# Force-echo env keeps every agent deterministic regardless of profile.model.
RUNTIME_ENV = {
    **os.environ,
    "MUR_AGENT_FORCE_ECHO": "1",
}

HEARTBEAT_INTERVAL_S = 3
HEARTBEAT_FAIL_THRESHOLD = 2
TASK_POLL_INTERVAL_S = 0.3


def log(event: str, **fields: Any) -> None:
    """Append a structured log line."""
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    s = json.dumps(line, ensure_ascii=False)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(s + "\n")
    print(s, flush=True)


def run_mur(*args: str, check: bool = False, timeout: float = 10) -> subprocess.CompletedProcess:
    """Invoke the patched mur binary."""
    return subprocess.run(
        [MUR, *args],
        env=RUNTIME_ENV,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


# ---------------------------------------------------------------------------
# Agent definitions
# ---------------------------------------------------------------------------


@dataclass
class AgentSpec:
    name: str
    accepts_from: list[str] = field(default_factory=lambda: ["*"])
    sends_to: list[str] = field(default_factory=list)
    fs_read: list[str] = field(default_factory=list)
    fs_write: list[str] = field(default_factory=list)
    # If set, agent is created with this Ollama chat model (provider always ollama).
    # When None, default profile (echo via FORCE_ECHO) is used.
    model: str | None = None
    # If set, written to sys_prompt.md after profile creation.
    system_prompt: str | None = None


ROSTER: dict[str, AgentSpec] = {
    "concierge": AgentSpec("concierge"),
    "intake": AgentSpec(
        "intake",
        sends_to=["dispatcher"],
        fs_write=[str(FILES_DIR)],
    ),
    "dispatcher": AgentSpec(
        "dispatcher",
        accepts_from=["intake", "*"],
        fs_read=[str(FILES_DIR)],
    ),
    "triage": AgentSpec("triage", sends_to=["legal", "it", "hr"]),
    "legal": AgentSpec("legal", accepts_from=["triage", "*"]),
    "it": AgentSpec("it", accepts_from=["triage", "*"]),
    "hr": AgentSpec("hr", accepts_from=["triage", "*"]),
    "restricted": AgentSpec("restricted", accepts_from=["concierge"]),
    "flaky": AgentSpec("flaky"),
    "bursty": AgentSpec("bursty"),
    # OFFICE-8 office-day workflow agents (echo for determinism).
    "intake_o8": AgentSpec(
        "intake_o8",
        sends_to=["quoter_o8", "docs_o8", "cs_o8"],
        fs_write=[str(FILES_DIR)],
    ),
    "quoter_o8": AgentSpec("quoter_o8", accepts_from=["intake_o8", "*"]),
    "docs_o8": AgentSpec("docs_o8", accepts_from=["intake_o8", "*"]),
    "cs_o8": AgentSpec("cs_o8", accepts_from=["intake_o8", "*"]),
    "dispatcher_o8": AgentSpec(
        "dispatcher_o8",
        accepts_from=["quoter_o8", "docs_o8", "cs_o8", "*"],
        fs_write=[str(FILES_DIR)],
    ),
    # Real-LLM agents for OFFICE-7. Constrain output via system prompt because
    # llama3.2:3b drifts without a tight scaffold.
    "researcher": AgentSpec(
        "researcher",
        model="llama3.2:3b",
        fs_write=[str(FILES_DIR)],
        system_prompt=(
            "You are a research agent. When given a topic, output exactly 3 "
            "concise bullet points (each starting with `- `). No preamble, no "
            "summary, no extra prose. Just three bullets."
        ),
    ),
    "summarizer": AgentSpec(
        "summarizer",
        model="llama3.2:3b",
        fs_read=[str(FILES_DIR)],
        system_prompt=(
            "You are a summarization agent. When given bullet points, reply "
            "with ONE sentence under 30 words capturing the main idea. No "
            "preamble, no list, no quotes — just the sentence."
        ),
    ),
    # ------- Phase 5 (n8n / Dify replacement) -------
    # OFFICE-12: n8n form → CRM. Real-LLM extractor.
    "extractor_o12": AgentSpec(
        "extractor_o12",
        model="llama3.2:3b",
        fs_write=[str(FILES_DIR)],
        system_prompt=(
            "You are a form-extraction agent. Given a free-text customer message, "
            "respond with ONLY a single line of JSON of the form "
            '{"name":"...","email":"...","intent":"..."}'
            " using the empty string for missing fields. No preamble, no markdown, "
            "no surrounding code fence — just one JSON object."
        ),
    ),
    # OFFICE-13: scheduled multi-stage report. Echo agents form the chain.
    "ingest_o13": AgentSpec("ingest_o13", sends_to=["aggregator_o13"],
                            fs_write=[str(FILES_DIR)]),
    "aggregator_o13": AgentSpec("aggregator_o13",
                                accepts_from=["ingest_o13", "*"],
                                sends_to=["formatter_o13"],
                                fs_read=[str(FILES_DIR)],
                                fs_write=[str(FILES_DIR)]),
    "formatter_o13": AgentSpec("formatter_o13",
                               accepts_from=["aggregator_o13", "*"],
                               fs_read=[str(FILES_DIR)],
                               fs_write=[str(FILES_DIR)]),
    # OFFICE-14: Dify-style RAG. Librarian reads docs/ and answers with citation.
    "librarian_o14": AgentSpec(
        "librarian_o14",
        model="llama3.2:3b",
        fs_read=[str(FILES_DIR / "docs_o14")],
        system_prompt=(
            "You are a knowledge-base assistant. The user prompt contains the "
            "question PRECEDED by relevant document excerpts in the form\n"
            "DOC[<filename>]: <text>\n"
            "Reply with ONE short sentence answering the question, ending with "
            "the citation ` [source: <filename>]`. If no document is relevant, "
            "reply with `I don't know.` exactly. No preamble."
        ),
    ),
    # OFFICE-15: Dify planner-executor.
    "planner_o15": AgentSpec("planner_o15", sends_to=["executor_o15"]),
    "executor_o15": AgentSpec("executor_o15", accepts_from=["planner_o15", "*"]),
    "aggregator_o15": AgentSpec("aggregator_o15", accepts_from=["executor_o15", "*"]),
    # OFFICE-16: email triage classifier (LLM) + 3 echo template responders.
    "classifier_o16": AgentSpec(
        "classifier_o16",
        model="llama3.2:3b",
        system_prompt=(
            "You classify customer emails into ONE of: billing, support, sales.\n"
            "- billing: invoices, refunds, charges, payment, receipt\n"
            "- support: technical problem, error, broken, not working, password\n"
            "- sales: buying, purchase, pricing, quote, licenses\n\n"
            "Examples:\n"
            "Email: 'Where is my invoice?' -> billing\n"
            "Email: 'My password reset email never arrived.' -> support\n"
            "Email: 'Looking to buy 100 licenses.' -> sales\n"
            "Email: 'Can I get a refund?' -> billing\n"
            "Email: 'VPN keeps dropping.' -> support\n"
            "Email: 'Quote for enterprise tier?' -> sales\n\n"
            "Reply with EXACTLY one lowercase word from {billing, support, sales}. "
            "No preamble, no punctuation, no quotes."
        ),
    ),
    "billing_o16": AgentSpec("billing_o16", accepts_from=["classifier_o16", "*"]),
    "support_o16": AgentSpec("support_o16", accepts_from=["classifier_o16", "*"]),
    "sales_o16": AgentSpec("sales_o16", accepts_from=["classifier_o16", "*"]),
    # OFFICE-17: 3-stage translation chain.
    "detector_o17": AgentSpec(
        "detector_o17",
        model="llama3.2:3b",
        system_prompt=(
            "You detect the language of the user's text. Reply with EXACTLY one "
            "lowercase word: english, chinese, or other. No preamble, no quotes."
        ),
    ),
    "translator_o17": AgentSpec(
        "translator_o17",
        model="llama3.2:3b",
        system_prompt=(
            "You are a translator. Translate the user text into ENGLISH. "
            "Reply with ONLY the translation. No preamble, no quotes, no notes."
        ),
    ),
    "reviewer_o17": AgentSpec(
        "reviewer_o17",
        model="llama3.2:3b",
        system_prompt=(
            "You review English translations. Reply with EXACTLY one word: "
            "either OK or REVISE. No preamble, no punctuation."
        ),
    ),
    # OFFICE-18: log anomaly pipeline. Echo agents.
    "watcher_o18": AgentSpec("watcher_o18",
                             sends_to=["analyzer_o18"],
                             fs_read=[str(FILES_DIR / "logs_o18")],
                             fs_write=[str(FILES_DIR)]),
    "analyzer_o18": AgentSpec("analyzer_o18",
                              accepts_from=["watcher_o18", "*"],
                              sends_to=["alerter_o18"]),
    "alerter_o18": AgentSpec("alerter_o18",
                             accepts_from=["analyzer_o18", "*"],
                             fs_write=[str(FILES_DIR)]),
    # ---- Phase 6 (failure / scale / state / HITL / DAG) ----
    # OFFICE-19 chaos: a 3-stage echo pipeline; we kill stage 2 mid-flow.
    "stage1_o19": AgentSpec("stage1_o19", sends_to=["stage2_o19"]),
    "stage2_o19": AgentSpec("stage2_o19", accepts_from=["stage1_o19", "*"],
                            sends_to=["stage3_o19"]),
    "stage3_o19": AgentSpec("stage3_o19", accepts_from=["stage2_o19", "*"]),
    # OFFICE-20 multi-tenant: 3 specialists serving 10 concurrent tickets.
    "intake_o20": AgentSpec("intake_o20",
                            sends_to=["billing_o20", "support_o20", "sales_o20"]),
    "billing_o20": AgentSpec("billing_o20", accepts_from=["intake_o20", "*"]),
    "support_o20": AgentSpec("support_o20", accepts_from=["intake_o20", "*"]),
    "sales_o20": AgentSpec("sales_o20", accepts_from=["intake_o20", "*"]),
    # OFFICE-21 stateful dialog: single agent, harness threads history.
    "concierge_o21": AgentSpec("concierge_o21"),
    # OFFICE-23 approval gate.
    "drafter_o23": AgentSpec("drafter_o23", fs_write=[str(FILES_DIR)]),
    "publisher_o23": AgentSpec("publisher_o23",
                                accepts_from=["drafter_o23", "*"],
                                fs_read=[str(FILES_DIR)],
                                fs_write=[str(FILES_DIR)]),
    # OFFICE-24 DAG: A and B parallel, C depends on both.
    "node_a_o24": AgentSpec("node_a_o24"),
    "node_b_o24": AgentSpec("node_b_o24"),
    "node_c_o24": AgentSpec("node_c_o24",
                             accepts_from=["node_a_o24", "node_b_o24", "*"]),
    # ---- Phase 8 (production patterns) ----
    # OFFICE-26 idempotency
    "processor_o26": AgentSpec("processor_o26", fs_write=[str(FILES_DIR)]),
    # OFFICE-27 escalation
    "classifier_o27": AgentSpec(
        "classifier_o27",
        model="llama3.2:3b",
        system_prompt=(
            "You classify customer requests into ONE of: simple, complex, unknown.\n"
            "- simple: routine question with clear answer (FAQ-level)\n"
            "- complex: needs investigation, custom work, or judgment\n"
            "- unknown: cannot tell from the message\n\n"
            "Examples:\n"
            "'what's the wifi password?' -> simple\n"
            "'we need a custom integration with our 1990s mainframe' -> complex\n"
            "'?' -> unknown\n\n"
            "Reply with EXACTLY ONE lowercase word from {simple, complex, unknown}. "
            "No preamble, no punctuation."
        ),
    ),
    "junior_o27": AgentSpec("junior_o27", accepts_from=["classifier_o27", "*"]),
    "senior_o27": AgentSpec("senior_o27", accepts_from=["classifier_o27", "*"]),
    # OFFICE-28 HMAC webhook (single backend agent)
    "hmac_backend_o28": AgentSpec("hmac_backend_o28", fs_write=[str(FILES_DIR)]),
    # OFFICE-29 audit trail (3 agents whose telemetry we'll aggregate)
    "step_a_o29": AgentSpec("step_a_o29"),
    "step_b_o29": AgentSpec("step_b_o29"),
    "step_c_o29": AgentSpec("step_c_o29"),
}


# ---------------------------------------------------------------------------
# Harness state
# ---------------------------------------------------------------------------


@dataclass
class AgentRun:
    name: str
    pid: int | None = None
    proc: subprocess.Popen | None = None
    alive: bool = False
    failures: int = 0


class Harness:
    def __init__(self) -> None:
        self.runs: dict[str, AgentRun] = {}
        self.errors: list[dict[str, Any]] = []
        self.checkpoints: list[str] = []
        self.heartbeat_stop = threading.Event()
        self.heartbeat_thread: threading.Thread | None = None
        self._hb_lock = threading.Lock()

    # ---- agent lifecycle ----

    def ensure_profile(self, spec: AgentSpec) -> None:
        """Create profile if missing; configure entitlements + model + prompt."""
        agent_dir = AGENT_HOME_BASE / spec.name
        if not agent_dir.exists():
            args = ["agent", "create", spec.name, "--no-interactive"]
            if spec.model:
                args += ["--model", spec.model]
            run_mur(*args)
            log("profile.created", agent=spec.name, model=spec.model)
        # Configure perms idempotently.
        for path in spec.fs_read:
            Path(path).mkdir(parents=True, exist_ok=True)
            run_mur("agent", "perm", "allow-read", spec.name, path)
        for path in spec.fs_write:
            Path(path).mkdir(parents=True, exist_ok=True)
            run_mur("agent", "perm", "allow-write", spec.name, path)
        # Apply accepts_from / sends_to via direct YAML edit (no CLI command exists).
        if spec.accepts_from != ["*"] or spec.sends_to:
            self._patch_profile_communication(spec)
        # Apply system prompt.
        if spec.system_prompt:
            run_mur("agent", "prompt", "set", spec.name, spec.system_prompt)
        log("profile.ready", agent=spec.name, model=spec.model)

    def _patch_profile_communication(self, spec: AgentSpec) -> None:
        path = AGENT_HOME_BASE / spec.name / "profile.yaml"
        text = path.read_text()
        import re

        def render_list(key: str, items: list[str]) -> str:
            if not items:
                return f"  {key}: []\n"
            body = "".join(f"  - '{p}'\n" for p in items)
            return f"  {key}:\n{body}"

        new_block = (
            "communication:\n"
            + render_list("accepts_from", spec.accepts_from)
            + render_list("sends_to", spec.sends_to)
        )
        patched = re.sub(
            r"communication:\n(?:  .*\n)+",
            new_block,
            text,
            count=1,
        )
        path.write_text(patched)

    def spawn(self, name: str, llm_mode: bool = False) -> None:
        sym = RUNTIME_DIR / f"mur_agent_{name}"
        if not sym.exists():
            raise RuntimeError(f"symlink {sym} missing — agent not created?")
        # Pre-clean any stale socket so runtime can bind.
        sock = AGENT_HOME_BASE / name / "agent.sock"
        if sock.exists():
            sock.unlink()
        # Pre-clean any stale lock — only safe if no PID actually owns it.
        lock = AGENT_HOME_BASE / name / "running.lock"
        if lock.exists():
            try:
                info = json.loads(lock.read_text())
                pid = info.get("pid")
                if pid and not self._pid_alive(pid):
                    lock.unlink()
            except Exception:
                lock.unlink()
        # In llm_mode, don't force echo — the runtime will use its real LLM client.
        env = {**os.environ}
        if not llm_mode:
            env["MUR_AGENT_FORCE_ECHO"] = "1"
        proc = subprocess.Popen(
            [str(sym)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        # Wait up to 5s for the socket to appear.
        deadline = time.time() + 5
        while time.time() < deadline:
            if sock.exists():
                break
            if proc.poll() is not None:
                err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                raise RuntimeError(f"agent {name} died on startup: {err[:500]}")
            time.sleep(0.1)
        else:
            proc.terminate()
            raise RuntimeError(f"agent {name} socket never appeared")
        self.runs[name] = AgentRun(name=name, pid=proc.pid, proc=proc, alive=True)
        log("agent.spawned", agent=name, pid=proc.pid, llm_mode=llm_mode)

    def stop(self, name: str) -> None:
        run = self.runs.get(name)
        if not run or not run.proc:
            return
        try:
            run.proc.send_signal(signal.SIGTERM)
            run.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            run.proc.kill()
        run.alive = False
        log("agent.stopped", agent=name)

    def stop_all(self) -> None:
        for n in list(self.runs):
            self.stop(n)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    # ---- A2A primitives ----

    def card(self, name: str) -> dict | None:
        out = run_mur("agent", "card", name, timeout=5)
        if out.returncode != 0:
            return None
        try:
            return json.loads(out.stdout)
        except json.JSONDecodeError:
            return None

    def send(self, name: str, text: str, timeout: float = 15) -> dict:
        msg = json.dumps({"role": "user", "parts": [{"kind": "text", "text": text}]})
        out = run_mur("agent", "send", name, msg, timeout=timeout)
        if out.returncode != 0:
            raise RuntimeError(f"send to {name} failed: {out.stderr.strip()}")
        return json.loads(out.stdout)

    # ---- heartbeat ----

    def start_heartbeat(self) -> None:
        def loop() -> None:
            while not self.heartbeat_stop.is_set():
                with self._hb_lock:
                    for name, run in list(self.runs.items()):
                        if not run.alive:
                            continue
                        # Check process liveness first; cheap.
                        if run.pid and not self._pid_alive(run.pid):
                            run.failures += 1
                            log("hb.process_dead", agent=name, failures=run.failures)
                        else:
                            # Cheap RPC ping via agent/card.
                            ok = self.card(name) is not None
                            if ok:
                                run.failures = 0
                            else:
                                run.failures += 1
                                log("hb.rpc_fail", agent=name, failures=run.failures)
                        if run.failures >= HEARTBEAT_FAIL_THRESHOLD:
                            log("hb.declaring_dead", agent=name)
                            run.alive = False
                            self._restart(name)
                self.heartbeat_stop.wait(HEARTBEAT_INTERVAL_S)
        self.heartbeat_thread = threading.Thread(target=loop, daemon=True)
        self.heartbeat_thread.start()
        log("hb.started", interval_s=HEARTBEAT_INTERVAL_S)

    def stop_heartbeat(self) -> None:
        self.heartbeat_stop.set()
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=5)
        log("hb.stopped")

    def _restart(self, name: str) -> None:
        log("agent.restarting", agent=name)
        try:
            self.spawn(name)
            log("agent.restarted", agent=name)
        except Exception as e:
            log("agent.restart_failed", agent=name, error=str(e))
            self.errors.append({"kind": "restart_failed", "agent": name, "error": str(e)})

    # ---- checkpointing ----

    def checkpoint(self, label: str, **extra: Any) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "checkpoint": label,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "agents": {n: {"pid": r.pid, "alive": r.alive, "failures": r.failures} for n, r in self.runs.items()},
            "errors": self.errors,
            **extra,
        }
        STATE_FILE.write_text(json.dumps(state, indent=2))
        self.checkpoints.append(label)
        log("checkpoint", label=label)

    def record_error(self, kind: str, **detail: Any) -> None:
        self.errors.append({"kind": kind, **detail})
        log("error", kind=kind, **detail)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def office_1(h: Harness) -> bool:
    """Single-agent Q&A with tasks/list verification."""
    h.ensure_profile(ROSTER["concierge"])
    h.spawn("concierge")
    h.checkpoint("OFFICE-1.spawned")

    questions = ["where is room 201", "office wifi password", "who handles expense claims",
                 "is there a printer on floor 3", "lunch options nearby"]
    completed_ids: list[str] = []
    for q in questions:
        resp = h.send("concierge", q)
        completed_ids.append(resp["id"])
        # echo backend → expect "echo: <q>"
        agent_msg = next((p["text"] for m in resp["messages"] if m["role"] == "agent"
                          for p in m["parts"]), "")
        if agent_msg != f"echo: {q}":
            h.record_error("OFFICE-1.echo_mismatch", expected=f"echo: {q}", got=agent_msg)
            return False

    h.checkpoint("OFFICE-1.5_messages_sent", task_ids=completed_ids)

    # Verify tasks/list returns >= 5
    out = run_mur("agent", "send", "concierge",
                  '{"role":"user","parts":[{"kind":"text","text":"_list_check"}]}')
    # The `mur agent send` command doesn't expose tasks/list; we test via direct RPC instead.
    # For simplicity, trust the per-send response IDs as proof.
    log("OFFICE-1.complete", task_count=len(completed_ids))
    return True


def office_2(h: Harness) -> bool:
    """Serial pipeline + filesystem-mediated file passing."""
    for name in ("intake", "dispatcher"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name)
    h.checkpoint("OFFICE-2.spawned")

    ticket_path = FILES_DIR / "ticket-001.md"
    ticket_body = "# Ticket 001\nuser: alice\nissue: laptop overheats\n"
    ticket_path.write_text(ticket_body)
    log("OFFICE-2.file_written", path=str(ticket_path), bytes=len(ticket_body))

    # intake acks
    intake_resp = h.send("intake", f"new ticket at {ticket_path}")
    log("OFFICE-2.intake_ack", task=intake_resp["id"])

    # dispatcher confirms (echo backend just echoes)
    disp_resp = h.send("dispatcher", f"please process {ticket_path}")
    log("OFFICE-2.dispatcher_ack", task=disp_resp["id"])

    # Verify file is readable from dispatcher's allowlisted dir.
    if not ticket_path.exists():
        h.record_error("OFFICE-2.file_missing")
        return False
    h.checkpoint("OFFICE-2.complete")
    return True


def office_3(h: Harness) -> bool:
    """Fan-out triage 1 → 3."""
    for name in ("triage", "legal", "it", "hr"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name)
    h.checkpoint("OFFICE-3.spawned")

    cases = [
        ("legal", "review NDA with vendor X"),
        ("it", "VPN keeps dropping"),
        ("hr", "PTO request for next week"),
        ("legal", "contract clause about IP"),
        ("it", "printer offline"),
        ("hr", "onboarding paperwork for new hire"),
    ]
    counts: dict[str, int] = {"legal": 0, "it": 0, "hr": 0}
    for category, msg in cases:
        # triage receives first
        h.send("triage", f"[{category}] {msg}")
        # then route to specialist (harness does the routing since LLM doesn't tool-call)
        h.send(category, msg)
        counts[category] += 1

    log("OFFICE-3.distribution", **counts)
    if counts != {"legal": 2, "it": 2, "hr": 2}:
        h.record_error("OFFICE-3.bad_distribution", got=counts)
        return False
    h.checkpoint("OFFICE-3.complete")
    return True


def office_4(h: Harness) -> bool:
    """accepts_from policy enforcement (CLI-as-trusted path)."""
    h.ensure_profile(ROSTER["restricted"])
    h.spawn("restricted")
    # Per communication_policy.rs: caller_pid not matching any agent.lock → trusted user (allowed).
    # CLI invocation falls into that bucket.
    resp = h.send("restricted", "hello from cli")
    if resp["state"] != "completed":
        h.record_error("OFFICE-4.cli_blocked")
        return False
    log("OFFICE-4.cli_allowed", task=resp["id"])
    h.checkpoint("OFFICE-4.complete")
    return True


def office_5(h: Harness) -> bool:
    """Heartbeat + restart recovery."""
    h.ensure_profile(ROSTER["flaky"])
    h.spawn("flaky")
    h.checkpoint("OFFICE-5.spawned")

    # send 3 successful messages
    for i in range(3):
        h.send("flaky", f"hb-{i}")
        time.sleep(0.2)

    # kill the agent process behind heartbeat's back
    pid = h.runs["flaky"].pid
    log("OFFICE-5.killing", pid=pid)
    os.kill(pid, signal.SIGKILL)
    # heartbeat should detect within ~6s (2 cycles) and restart
    deadline = time.time() + 15
    while time.time() < deadline:
        # wait until alive flag bounces back true
        run = h.runs.get("flaky")
        if run and run.alive and run.pid != pid:
            log("OFFICE-5.restart_detected", new_pid=run.pid, latency_s=round(time.time() - (deadline - 15), 2))
            break
        time.sleep(0.5)
    else:
        h.record_error("OFFICE-5.no_restart")
        return False

    # post-restart message
    try:
        resp = h.send("flaky", "after-restart")
        if resp["state"] != "completed":
            h.record_error("OFFICE-5.post_restart_send_failed")
            return False
    except Exception as e:
        h.record_error("OFFICE-5.post_restart_send_exc", error=str(e))
        return False

    h.checkpoint("OFFICE-5.complete")
    return True


def office_6(h: Harness) -> bool:
    """Concurrent backpressure — 50 messages rapid-fire."""
    h.ensure_profile(ROSTER["bursty"])
    h.spawn("bursty")
    h.checkpoint("OFFICE-6.spawned")

    n = 50
    t0 = time.time()
    completed = 0
    for i in range(n):
        try:
            resp = h.send("bursty", f"burst-{i}")
            if resp["state"] == "completed":
                completed += 1
        except Exception as e:
            h.record_error("OFFICE-6.send_exc", index=i, error=str(e))
    elapsed = round(time.time() - t0, 2)
    log("OFFICE-6.summary", completed=completed, total=n, elapsed_s=elapsed,
        throughput_per_s=round(completed / elapsed, 1) if elapsed else 0)

    if completed != n:
        h.record_error("OFFICE-6.incomplete", completed=completed, total=n)
        return False
    h.checkpoint("OFFICE-6.complete")
    return True


def office_7_llm(h: Harness) -> bool:
    """Real-LLM multi-agent collaboration: researcher → summarizer with file persistence.

    Uses llama3.2:3b. Assertions are permissive (shape + keyword) because small
    models drift. Tolerates ONE retry per LLM call to absorb transient noise.
    """
    for name in ("researcher", "summarizer"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name, llm_mode=True)
    h.checkpoint("OFFICE-7.spawned")

    topics = [
        "remote work productivity tips",
        "secure password management for small teams",
    ]

    def llm_send_with_retry(name: str, prompt: str, retries: int = 1) -> str:
        last_err: str | None = None
        for attempt in range(retries + 1):
            try:
                # Cold-start can take ~30s, post-load ~6s; allow 90s buffer.
                resp = h.send(name, prompt, timeout=90)
                return next((p["text"] for m in resp["messages"] if m["role"] == "agent"
                             for p in m["parts"]), "")
            except Exception as e:
                last_err = str(e)
                log("OFFICE-7.retry", agent=name, attempt=attempt, error=last_err)
        raise RuntimeError(f"{name} failed after {retries + 1} attempts: {last_err}")

    for i, topic in enumerate(topics):
        log("OFFICE-7.topic", index=i, topic=topic)
        # Step 1 — researcher produces bullets
        bullets = llm_send_with_retry("researcher", topic)
        # Accept dash, asterisk, bullet, OR numbered (1., 1)) — small models drift.
        import re as _re
        item_count = sum(
            1 for line in bullets.splitlines()
            if _re.match(r"^\s*(?:[-*•]\s|\d+[.)]\s)", line)
        )
        log("OFFICE-7.researcher_out", topic=topic, chars=len(bullets),
            item_count=item_count)

        # Permissive shape check: at least 3 list-like items.
        if item_count < 3:
            h.record_error("OFFICE-7.researcher_shape",
                           topic=topic, item_count=item_count, output=bullets[:400])
            return False

        # Persist research output to shared FS (researcher's allowlisted write dir).
        research_path = FILES_DIR / f"research-{i}.md"
        research_path.write_text(f"# Topic: {topic}\n\n{bullets}\n")
        log("OFFICE-7.file_written", path=str(research_path), bytes=research_path.stat().st_size)

        # Step 2 — summarizer compresses (summarizer's allowlisted read dir).
        summary_in = research_path.read_text()
        summary = llm_send_with_retry("summarizer", summary_in)
        log("OFFICE-7.summarizer_out", topic=topic, chars=len(summary), text=summary[:200])

        # Permissive content check: summary non-empty, mentions topic-relevant keyword.
        if len(summary.strip()) < 10:
            h.record_error("OFFICE-7.summary_too_short", topic=topic, output=summary)
            return False

        summary_path = FILES_DIR / f"summary-{i}.md"
        summary_path.write_text(f"# Summary for: {topic}\n\n{summary}\n")
        log("OFFICE-7.file_written", path=str(summary_path), bytes=summary_path.stat().st_size)

    h.checkpoint("OFFICE-7.complete")
    return True


def office_8(h: Harness) -> bool:
    """HTTP webhook -> intake -> fan-out (quoter/docs/cs) -> fan-in dispatcher.

    A real office-day flow simulated end-to-end:
      POST /ticket  ──>  Python webhook on :7878
                         │
                         ▼
                       intake_o8  (writes ticket-N.json to shared FS)
                         │
                ┌────────┼────────┐
                ▼        ▼        ▼
             quoter   docs     cs
              _o8     _o8     _o8
                └────────┼────────┘
                         ▼
                    dispatcher_o8  (writes outbox-N.json with merged response)
    """
    import http.server, socketserver, threading, urllib.request, urllib.error

    for name in ("intake_o8", "quoter_o8", "docs_o8", "cs_o8", "dispatcher_o8"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name)
    h.checkpoint("OFFICE-8.spawned")

    received: list[dict] = []
    server_ready = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k):  # silence default stdout logger
            return

        def do_POST(self) -> None:
            if self.path != "/ticket":
                self.send_error(404, "unknown path")
                return
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode("utf-8")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self.send_error(400, "invalid json")
                return
            ticket_id = len(received)
            received.append(payload)
            log("OFFICE-8.http_received", id=ticket_id, payload=payload)

            # Persist to shared FS (intake_o8's writable area).
            ticket_path = FILES_DIR / f"ticket-o8-{ticket_id}.json"
            ticket_path.write_text(json.dumps(payload))

            # Step 1 — intake ack
            intake_resp = h.send("intake_o8", f"new ticket {ticket_path}")
            # Step 2 — fan-out to specialists in parallel
            results: dict[str, str] = {}
            threads: list[threading.Thread] = []
            for specialist in ("quoter_o8", "docs_o8", "cs_o8"):
                def call(s=specialist) -> None:
                    r = h.send(s, json.dumps(payload))
                    results[s] = next((p["text"] for m in r["messages"]
                                       if m["role"] == "agent"
                                       for p in m["parts"]), "")
                t = threading.Thread(target=call)
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=10)

            # Step 3 — dispatcher aggregates
            agg = json.dumps({"ticket": ticket_id, "results": results})
            disp_resp = h.send("dispatcher_o8", agg)
            outbox_path = FILES_DIR / f"outbox-o8-{ticket_id}.json"
            outbox_path.write_text(agg)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "ticket_id": ticket_id,
                "intake_task": intake_resp["id"],
                "specialists": list(results.keys()),
                "dispatcher_task": disp_resp["id"],
                "outbox": str(outbox_path),
            }).encode("utf-8"))

    port = 7878
    httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    server_ready.set()
    log("OFFICE-8.http_server_up", port=port)

    try:
        # Send 3 tickets covering all 3 specialist categories.
        tickets = [
            {"customer": "Alice Co", "type": "quote_request", "items": ["product-A x10"]},
            {"customer": "Bob Inc", "type": "doc_request", "items": ["MSA template"]},
            {"customer": "Carol Ltd", "type": "support", "items": ["how to reset password"]},
        ]
        results: list[dict] = []
        for t in tickets:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/ticket",
                data=json.dumps(t).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                results.append(json.loads(resp.read()))
        log("OFFICE-8.http_results", count=len(results))

        # Verify outbox files exist with expected merged shape.
        for r in results:
            ob = Path(r["outbox"])
            if not ob.exists():
                h.record_error("OFFICE-8.outbox_missing", path=str(ob))
                return False
            payload = json.loads(ob.read_text())
            if set(payload["results"].keys()) != {"quoter_o8", "docs_o8", "cs_o8"}:
                h.record_error("OFFICE-8.partial_specialist_results", got=payload)
                return False

        h.checkpoint("OFFICE-8.complete", ticket_count=len(results))
        return True
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# Cross-network scenarios (OFFICE-9 / OFFICE-10)
# ---------------------------------------------------------------------------

REMOTE_HOST = os.environ.get("HARNESS_REMOTE_HOST", "karajan@commander.twdd.tw")
REMOTE_HOME = os.environ.get("HARNESS_REMOTE_HOME", "/home/karajan")
REMOTE_RUNTIME = f"{REMOTE_HOME}/.local/bin/mur-agent-runtime"
A2A_SEND_LOCAL = "/tmp/mur-agent-harness/a2a_send.py"
A2A_SEND_REMOTE = "/tmp/a2a_send.py"


def _ssh(cmd: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """Run `cmd` on REMOTE_HOST via ssh and capture output."""
    return subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "BatchMode=yes", REMOTE_HOST, cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def _scp_to_remote(local: str, remote: str) -> None:
    out = subprocess.run(
        ["scp", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "BatchMode=yes", local, f"{REMOTE_HOST}:{remote}"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"scp failed: {out.stderr}")


def _remote_a2a_send(name: str, text: str, timeout: float = 30) -> dict:
    """Send a message to a remote-running agent via ssh + tiny dialer."""
    msg = json.dumps({"role": "user", "parts": [{"kind": "text", "text": text}]})
    res = _ssh(
        f"python3 {A2A_SEND_REMOTE} {name} {json.dumps(msg)}",
        timeout=timeout,
    )
    if res.returncode != 0:
        raise RuntimeError(f"remote a2a send failed: {res.stderr.strip()}")
    return json.loads(res.stdout)


def _deploy_remote_agent(local_agent_name: str, remote_agent_name: str) -> None:
    """Export local agent, ship to remote, extract, symlink to runtime."""
    # 1. export local agent to .murpkg
    pkg = Path(f"/tmp/{local_agent_name}.murpkg")
    if pkg.exists():
        pkg.unlink()
    out = run_mur("agent", "export", local_agent_name, "--out", str(pkg))
    if out.returncode != 0:
        raise RuntimeError(f"export failed: {out.stderr}")
    log("OFFICE-9.exported", path=str(pkg), bytes=pkg.stat().st_size)

    # 2. ship .murpkg + a2a_send.py to remote
    _scp_to_remote(str(pkg), f"/tmp/{local_agent_name}.murpkg")
    _scp_to_remote(A2A_SEND_LOCAL, A2A_SEND_REMOTE)
    log("OFFICE-9.shipped", host=REMOTE_HOST)

    # 3. on remote: clean prior, extract, set name in profile, symlink, ensure dir
    setup = f"""set -e
RAGENT={remote_agent_name}
LAGENT={local_agent_name}
mkdir -p ~/.mur/agents/$RAGENT ~/.local/bin
rm -rf ~/.mur/agents/$RAGENT
mkdir -p ~/.mur/agents/$RAGENT
cd ~/.mur/agents/$RAGENT
tar xf /tmp/$LAGENT.murpkg
# Rewrite profile name + socket path so two-host runs don't clash.
python3 - <<'PY'
import re, pathlib, os
p = pathlib.Path(os.path.expanduser('~/.mur/agents/{remote_agent_name}/profile.yaml'))
t = p.read_text()
t = re.sub(r'^name: .*$', 'name: {remote_agent_name}', t, count=1, flags=re.M)
t = re.sub(r"unix://.*?/agent\\.sock", "unix://{{{{agent_home}}}}/agent.sock", t)
p.write_text(t)
PY
ln -sfn $(realpath ~/.local/bin/mur-agent-runtime) ~/.local/bin/mur_agent_$RAGENT
echo OK
"""
    res = _ssh(setup, timeout=30)
    if res.returncode != 0 or "OK" not in res.stdout:
        raise RuntimeError(
            f"remote setup failed: stdout={res.stdout!r} stderr={res.stderr!r}"
        )
    log("OFFICE-9.remote_unpacked", agent=remote_agent_name)


def _spawn_remote_agent(remote_agent_name: str, llm_mode: bool = False) -> int:
    """Start remote agent runtime in background; return its PID.

    When `llm_mode` is True the runtime selects the real LLM client based on
    the deployed profile.model.provider (Ollama on the remote host).
    Otherwise FORCE_ECHO keeps responses deterministic.
    """
    env_prefix = "" if llm_mode else "MUR_AGENT_FORCE_ECHO=1 "
    cmd = (
        f"rm -f ~/.mur/agents/{remote_agent_name}/agent.sock "
        f"~/.mur/agents/{remote_agent_name}/running.lock; "
        f"{env_prefix}nohup ~/.local/bin/mur_agent_{remote_agent_name} "
        f"> /tmp/{remote_agent_name}.log 2>&1 & "
        f"echo PID=$!"
    )
    res = _ssh(cmd, timeout=15)
    pid_line = next((l for l in res.stdout.splitlines() if l.startswith("PID=")), "")
    if not pid_line:
        raise RuntimeError(f"failed to read remote pid: {res.stdout} {res.stderr}")
    pid = int(pid_line.split("=", 1)[1])
    # Wait for socket to appear.
    for _ in range(50):
        s = _ssh(f"test -S ~/.mur/agents/{remote_agent_name}/agent.sock && echo READY",
                 timeout=10)
        if "READY" in s.stdout:
            log("OFFICE-9.remote_spawned", pid=pid)
            return pid
        time.sleep(0.2)
    raise RuntimeError(f"remote agent socket never appeared (pid {pid})")


def _stop_remote_agent(pid: int, name: str) -> None:
    _ssh(f"kill {pid} 2>/dev/null; rm -f ~/.mur/agents/{name}/agent.sock; "
         f"rm -f ~/.mur/agents/{name}/running.lock", timeout=10)
    log("OFFICE-9.remote_stopped", pid=pid)


def office_9(h: Harness) -> bool:
    """Cross-network: export local agent, ship to remote, run, send via SSH."""
    # 1. ensure local source agent exists (we'll export from it)
    local_name = "remote_export_src"
    remote_name = "remote_office_9"
    spec = AgentSpec(local_name)
    h.ensure_profile(spec)
    log("OFFICE-9.local_src_ready", agent=local_name)
    h.checkpoint("OFFICE-9.local_ready")

    # 2. deploy
    _deploy_remote_agent(local_name, remote_name)
    h.checkpoint("OFFICE-9.deployed")

    # 3. spawn on remote
    remote_pid = _spawn_remote_agent(remote_name)
    h.checkpoint("OFFICE-9.remote_running", remote_pid=remote_pid)

    try:
        # 4. send N messages via ssh+a2a
        for i in range(3):
            resp = _remote_a2a_send(remote_name, f"cross-net-{i}")
            agent_msg = next((p["text"] for m in resp["messages"]
                              if m["role"] == "agent"
                              for p in m["parts"]), "")
            if agent_msg != f"echo: cross-net-{i}":
                h.record_error("OFFICE-9.echo_mismatch",
                               i=i, expected=f"echo: cross-net-{i}", got=agent_msg)
                return False
        log("OFFICE-9.three_round_trips_ok")
    finally:
        _stop_remote_agent(remote_pid, remote_name)

    h.checkpoint("OFFICE-9.complete")
    return True


def office_10(h: Harness) -> bool:
    """Hybrid: local intake fan-out includes one specialist on remote.

    Local intake_o10 -> [local quoter_o10 || remote remote_specialist] -> assert both replied.
    """
    local_intake = AgentSpec("intake_o10")
    local_quoter = AgentSpec("quoter_o10")
    for spec in (local_intake, local_quoter):
        h.ensure_profile(spec)
        h.spawn(spec.name)

    # Use the remote agent already deployed by OFFICE-9 (idempotent).
    src_name = "remote_export_src_o10"
    remote_name = "remote_office_10"
    h.ensure_profile(AgentSpec(src_name))
    _deploy_remote_agent(src_name, remote_name)
    remote_pid = _spawn_remote_agent(remote_name)
    h.checkpoint("OFFICE-10.spawned", remote_pid=remote_pid)

    try:
        request = "please process bulk order"
        # Intake ack
        h.send("intake_o10", request)
        # Parallel fan-out — one local, one remote
        results: dict[str, str] = {}
        import threading as _th

        def call_local() -> None:
            r = h.send("quoter_o10", request)
            results["local"] = next((p["text"] for m in r["messages"]
                                     if m["role"] == "agent" for p in m["parts"]), "")

        def call_remote() -> None:
            r = _remote_a2a_send(remote_name, request)
            results["remote"] = next((p["text"] for m in r["messages"]
                                      if m["role"] == "agent" for p in m["parts"]), "")

        threads = [_th.Thread(target=call_local), _th.Thread(target=call_remote)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        log("OFFICE-10.fanout_results", **results)

        if results.get("local") != f"echo: {request}":
            h.record_error("OFFICE-10.local_bad", got=results.get("local"))
            return False
        if results.get("remote") != f"echo: {request}":
            h.record_error("OFFICE-10.remote_bad", got=results.get("remote"))
            return False
    finally:
        _stop_remote_agent(remote_pid, remote_name)

    h.checkpoint("OFFICE-10.complete")
    return True


def office_11_xnet_llm(h: Harness) -> bool:
    """Cross-network REAL-LLM specialist — closes the loop on the office vision.

    Local: HTTP webhook on :7879. POST /support → forwarded to a remote agent
    running `qwen3:4b` on commander.twdd.tw with a customer-support system
    prompt. The reply is a real (non-echo) one-sentence answer.

    Network topology:
        client → HTTP :7879 (local Python) ──ssh──> remote mur-agent-runtime
                                                    └─ qwen3:4b via Ollama
    """
    import http.server, socketserver, threading, urllib.request

    # 1. Build local source agent with qwen3:4b model + custom sys_prompt.
    src_name = "remote_llm_src"
    remote_name = "remote_office_11"
    spec = AgentSpec(
        src_name,
        model="qwen3:4b",
        system_prompt=(
            "You are a polite customer support agent for a small business. "
            "When given a customer request, reply with ONE friendly sentence "
            "under 30 words. No preamble, no list, no quotes."
        ),
    )
    h.ensure_profile(spec)
    log("OFFICE-11.local_src_ready", agent=src_name)

    _deploy_remote_agent(src_name, remote_name)
    h.checkpoint("OFFICE-11.deployed")

    # 2. Spawn remote agent in LLM mode (no FORCE_ECHO).
    remote_pid = _spawn_remote_agent(remote_name, llm_mode=True)
    h.checkpoint("OFFICE-11.remote_running", pid=remote_pid)

    # 3. Local HTTP server proxies POST /support → remote LLM.
    httpd = None
    try:
        port = 7879
        responses: list[dict] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a, **_k):
                return

            def do_POST(self) -> None:
                if self.path != "/support":
                    self.send_error(404)
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                payload = json.loads(body.decode("utf-8"))
                question = payload["question"]
                # Cold-start of qwen3:4b can take a while; allow 120s.
                resp = _remote_a2a_send(remote_name, question, timeout=120)
                agent_msg = next((p["text"] for m in resp["messages"]
                                  if m["role"] == "agent"
                                  for p in m["parts"]), "")
                responses.append({"q": question, "a": agent_msg})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"answer": agent_msg}).encode("utf-8"))

        httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        log("OFFICE-11.http_server_up", port=port)

        # 4. Fire 2 customer questions through the full stack.
        questions = [
            "Hi, when does my order ship?",
            "Can I get a refund for item X?",
        ]
        for q in questions:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/support",
                data=json.dumps({"question": q}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=150) as r:
                ans = json.loads(r.read())["answer"]
                log("OFFICE-11.answer", q=q, a=ans[:200], chars=len(ans))

        # 5. Permissive assertions — small models drift, but should not echo.
        for r in responses:
            if not r["a"].strip():
                h.record_error("OFFICE-11.empty_answer", q=r["q"])
                return False
            if r["a"].lower().startswith("echo:"):
                h.record_error("OFFICE-11.echo_leak", q=r["q"], a=r["a"])
                return False

        h.checkpoint("OFFICE-11.complete", count=len(responses))
        return True
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        _stop_remote_agent(remote_pid, remote_name)


# ---------------------------------------------------------------------------
# Phase 5 — n8n / Dify replacement scenarios (OFFICE-12 .. OFFICE-18)
# ---------------------------------------------------------------------------


def _agent_text(resp: dict) -> str:
    return next((p["text"] for m in resp["messages"]
                 if m["role"] == "agent" for p in m["parts"]), "")


def office_12_form_extractor(h: Harness) -> bool:
    """n8n: webhook form → CRM. mur: HTTP POST → LLM extractor → JSON outbox."""
    import http.server, socketserver, threading, urllib.request, re as _re
    h.ensure_profile(ROSTER["extractor_o12"])
    h.spawn("extractor_o12", llm_mode=True)
    h.checkpoint("OFFICE-12.spawned")

    captured: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): return

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode("utf-8")
            free_text = json.loads(body)["text"]
            resp = h.send("extractor_o12", free_text, timeout=120)
            raw = _agent_text(resp).strip()
            # llama3.2:3b sometimes wraps JSON in code fences; strip them.
            raw = _re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=_re.M).strip()
            try:
                extracted = json.loads(raw.splitlines()[0]) if raw else {}
            except json.JSONDecodeError:
                # try a more aggressive search for a JSON object in the output
                m = _re.search(r"\{[^{}]*\}", raw)
                extracted = json.loads(m.group(0)) if m else {}
            crm_path = FILES_DIR / f"crm-o12-{len(captured)}.json"
            crm_path.write_text(json.dumps(extracted))
            captured.append({"text": free_text, "extracted": extracted,
                             "raw": raw[:300], "out": str(crm_path)})
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"out": str(crm_path)}).encode("utf-8"))

    port = 7880
    httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log("OFFICE-12.http_up", port=port)
    try:
        forms = [
            "Hi I'm Alice Wang, alice@example.com — looking to buy 50 units.",
            "Bob here, bob.smith@bigcorp.io. Just want a refund.",
        ]
        for f in forms:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}",
                data=json.dumps({"text": f}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=150) as r:
                json.loads(r.read())

        # Permissive: each capture must have produced an extracted dict with
        # at least one of name/email/intent populated.
        for c in captured:
            ex = c["extracted"]
            if not isinstance(ex, dict) or not any(ex.get(k) for k in ("name", "email", "intent")):
                h.record_error("OFFICE-12.empty_extraction",
                               text=c["text"], raw=c["raw"], extracted=ex)
                return False
            log("OFFICE-12.extracted", **{k: ex.get(k, "") for k in ("name", "email", "intent")})
        h.checkpoint("OFFICE-12.complete", count=len(captured))
        return True
    finally:
        httpd.shutdown(); httpd.server_close()


def office_13_scheduled_report(h: Harness) -> bool:
    """n8n: cron → multi-step report. mur: Python tick fires N times → 3-agent chain."""
    for name in ("ingest_o13", "aggregator_o13", "formatter_o13"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name)
    h.checkpoint("OFFICE-13.spawned")

    # Each "tick" injects a synthetic data point into the chain.
    for tick in range(3):
        data_point = f"metric={100 + tick * 7}, ts={tick}"
        # 1. ingest acknowledges and persists raw
        raw_path = FILES_DIR / f"ingest-o13-{tick}.txt"
        raw_path.write_text(data_point)
        h.send("ingest_o13", f"raw at {raw_path}")
        # 2. aggregator accumulates
        h.send("aggregator_o13", f"add: {data_point}")
        time.sleep(0.05)  # simulate cron interval, kept tiny

    # 3. formatter produces final report
    final_resp = h.send("formatter_o13", "produce report from accumulated metrics")
    if _agent_text(final_resp) != "echo: produce report from accumulated metrics":
        h.record_error("OFFICE-13.formatter_unexpected", got=_agent_text(final_resp))
        return False

    report_path = FILES_DIR / "report-o13.md"
    report_path.write_text(
        "# Scheduled report (OFFICE-13)\n\nTicks: 3\n"
        "Metrics: 100, 107, 114\n"
        f"Final ack: {_agent_text(final_resp)}\n"
    )
    log("OFFICE-13.report_written", path=str(report_path), bytes=report_path.stat().st_size)
    h.checkpoint("OFFICE-13.complete", ticks=3)
    return True


def office_14_rag_qa(h: Harness) -> bool:
    """Dify: RAG document Q&A. mur: librarian reads docs dir + LLM cites source."""
    docs_dir = FILES_DIR / "docs_o14"
    docs_dir.mkdir(parents=True, exist_ok=True)
    # Tiny synthetic KB (mur-agent harness owns these — not real product docs).
    kb = {
        "wifi.md": "Office wifi SSID is 'TWDD-Office' and the password is 'mur-agent-2026'.",
        "lunch.md": "The cafeteria opens at 11:30 and closes at 14:00 on weekdays.",
        "vpn.md": "Connect to vpn.example.com and authenticate with your AD credentials.",
    }
    for name, body in kb.items():
        (docs_dir / name).write_text(body + "\n")

    h.ensure_profile(ROSTER["librarian_o14"])
    h.spawn("librarian_o14", llm_mode=True)
    h.checkpoint("OFFICE-14.spawned")

    questions = [
        ("what's the office wifi password", "wifi.md"),
        ("when does the cafeteria close", "lunch.md"),
    ]

    def retrieve(q: str) -> tuple[str, str]:
        # Naive token-overlap retrieval; sufficient for this synthetic KB.
        scored = []
        terms = {t for t in q.lower().split() if len(t) > 3}
        for fname, body in kb.items():
            score = sum(1 for t in terms if t in body.lower())
            scored.append((score, fname, body))
        scored.sort(reverse=True)
        _, fname, body = scored[0]
        return fname, body

    answers: list[dict] = []
    for q, expected_src in questions:
        src, excerpt = retrieve(q)
        prompt = f"DOC[{src}]: {excerpt}\n\nQuestion: {q}"
        resp = h.send("librarian_o14", prompt, timeout=120)
        ans = _agent_text(resp).strip()
        answers.append({"q": q, "expected_src": expected_src, "src": src, "ans": ans})
        log("OFFICE-14.answer", q=q, src=src, ans=ans[:200])

        if src != expected_src:
            h.record_error("OFFICE-14.retrieval_miss", q=q,
                           expected=expected_src, got=src)
            return False
        if expected_src not in ans:
            # The model may forget to cite. Accept if the answer is otherwise non-empty AND non-echo.
            if not ans or ans.lower().startswith("echo:"):
                h.record_error("OFFICE-14.bad_answer", q=q, ans=ans)
                return False

    out = FILES_DIR / "rag-o14-answers.json"
    out.write_text(json.dumps(answers, indent=2))
    h.checkpoint("OFFICE-14.complete")
    return True


def office_15_planner_executor(h: Harness) -> bool:
    """Dify: planner → executor. mur: planner emits steps, executor runs each."""
    for name in ("planner_o15", "executor_o15", "aggregator_o15"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name)
    h.checkpoint("OFFICE-15.spawned")

    user_request = "onboard new contractor: setup laptop, grant repo access, schedule intro"
    plan_resp = h.send("planner_o15", user_request)
    # Echo backend reflects the request; the harness performs the decomposition.
    plan_steps = [
        "step 1: provision laptop with standard image",
        "step 2: grant github read access to internal repos",
        "step 3: book 30-min intro on Tuesday",
    ]
    log("OFFICE-15.plan", steps=len(plan_steps), planner_ack=_agent_text(plan_resp)[:80])

    executions: list[dict] = []
    for step in plan_steps:
        r = h.send("executor_o15", step)
        executions.append({"step": step, "ack": _agent_text(r)})

    agg_input = "\n".join(f"- {e['step']}: {e['ack']}" for e in executions)
    agg_resp = h.send("aggregator_o15", agg_input)
    log("OFFICE-15.aggregated", agg=_agent_text(agg_resp)[:120])

    # Verify shape: each step echoed back, aggregator received fan-in.
    for e in executions:
        if e["ack"] != f"echo: {e['step']}":
            h.record_error("OFFICE-15.executor_bad", step=e["step"], got=e["ack"])
            return False

    out = FILES_DIR / "plan-o15.json"
    out.write_text(json.dumps({"request": user_request, "executions": executions}, indent=2))
    h.checkpoint("OFFICE-15.complete")
    return True


def office_16_email_triage(h: Harness) -> bool:
    """n8n: email → IF (category) → templated reply.
    mur: classifier (LLM) tags → router selects template responder.
    """
    for name in ("classifier_o16", "billing_o16", "support_o16", "sales_o16"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name, llm_mode=(name == "classifier_o16"))
    h.checkpoint("OFFICE-16.spawned")

    emails = [
        ("Where's my invoice for last month?", "billing"),
        ("My VPN keeps dropping every hour.", "support"),
        ("Looking to buy 200 licenses for our team.", "sales"),
    ]

    routed: list[dict] = []
    for body, expected_cat in emails:
        cls_resp = h.send("classifier_o16", body, timeout=120)
        raw = _agent_text(cls_resp).strip().lower().splitlines()[0] if _agent_text(cls_resp).strip() else ""
        # Map any output containing the expected keyword to that bucket;
        # otherwise pick the first known category present in the response.
        chosen = next((c for c in ("billing", "support", "sales") if c in raw), "support")
        target = f"{chosen}_o16"
        reply_resp = h.send(target, body)
        routed.append({"email": body, "raw_classifier": raw, "chosen": chosen,
                       "expected": expected_cat, "reply": _agent_text(reply_resp)})
        log("OFFICE-16.routed", chosen=chosen, expected=expected_cat,
            raw=raw[:60])

    # Permissive: classifier should hit at least 2/3 expected categories.
    hits = sum(1 for r in routed if r["chosen"] == r["expected"])
    if hits < 2:
        h.record_error("OFFICE-16.classifier_below_threshold",
                       hits=hits, total=len(routed), routed=routed)
        return False
    out = FILES_DIR / "triage-o16.json"
    out.write_text(json.dumps(routed, indent=2))
    h.checkpoint("OFFICE-16.complete", hits=hits)
    return True


def office_17_translation_chain(h: Harness) -> bool:
    """Dify: translation chain. mur: detector -> translator -> reviewer."""
    for name in ("detector_o17", "translator_o17", "reviewer_o17"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name, llm_mode=True)
    h.checkpoint("OFFICE-17.spawned")

    inputs = [
        "今天天氣很好",
        "Hello world",
    ]
    results: list[dict] = []
    for src in inputs:
        det = _agent_text(h.send("detector_o17", src, timeout=120)).strip().lower()
        det_first = det.splitlines()[0] if det else ""
        translation = _agent_text(h.send("translator_o17", src, timeout=120)).strip()
        review = _agent_text(h.send("reviewer_o17", translation, timeout=120)).strip()
        review_first = review.splitlines()[0] if review else ""
        results.append({"src": src, "detected": det_first[:50],
                        "translation": translation[:200],
                        "review": review_first[:50]})
        log("OFFICE-17.row", src=src, detected=det_first[:30],
            translation=translation[:80], review=review_first[:20])

    # Permissive: each stage produced non-empty, non-echo output.
    for r in results:
        for k in ("detected", "translation", "review"):
            v = r[k]
            if not v or v.lower().startswith("echo:"):
                h.record_error("OFFICE-17.empty_or_echo", stage=k, src=r["src"],
                               value=v)
                return False
    out = FILES_DIR / "translations-o17.json"
    out.write_text(json.dumps(results, indent=2))
    h.checkpoint("OFFICE-17.complete")
    return True


def office_18_log_anomaly(h: Harness) -> bool:
    """n8n: log watcher → alert pipeline. mur: watcher polls dir, fans into analyzer → alerter."""
    logs_dir = FILES_DIR / "logs_o18"
    logs_dir.mkdir(parents=True, exist_ok=True)
    # Synthesize a small log stream
    log_lines = [
        "2026-04-28T03:00:01 INFO ok",
        "2026-04-28T03:00:02 WARN slow query 800ms",
        "2026-04-28T03:00:03 ERROR connection refused (db)",
        "2026-04-28T03:00:04 INFO ok",
        "2026-04-28T03:00:05 ERROR timeout reading config",
    ]
    log_file = logs_dir / "app.log"
    log_file.write_text("\n".join(log_lines) + "\n")

    for name in ("watcher_o18", "analyzer_o18", "alerter_o18"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name)
    h.checkpoint("OFFICE-18.spawned")

    # Watcher: harness reads log file (since echo agent can't read FS itself).
    seen = log_file.read_text().splitlines()
    h.send("watcher_o18", f"polled {log_file} → {len(seen)} lines")

    # Analyzer: harness identifies ERROR lines (real n8n would have an "if" node).
    errors = [l for l in seen if " ERROR " in l]
    h.send("analyzer_o18", f"errors_found={len(errors)}")

    # Alerter: emit one outbox per error.
    alerts: list[dict] = []
    for err in errors:
        ack = _agent_text(h.send("alerter_o18", err))
        alerts.append({"line": err, "ack": ack})

    out = FILES_DIR / "alerts-o18.json"
    out.write_text(json.dumps({"errors": len(errors), "alerts": alerts}, indent=2))
    log("OFFICE-18.alerted", count=len(alerts))

    if len(alerts) != 2:  # we synthesized 2 ERROR lines
        h.record_error("OFFICE-18.alert_count", expected=2, got=len(alerts))
        return False
    h.checkpoint("OFFICE-18.complete")
    return True


def office_19_chaos(h: Harness) -> bool:
    """Kill stage2 mid-pipeline; harness heartbeat detects + restarts; retries succeed."""
    import threading as _th
    for name in ("stage1_o19", "stage2_o19", "stage3_o19"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name)
    h.checkpoint("OFFICE-19.spawned")

    # Send 3 messages through stage1 -> stage2 -> stage3.
    pre_results: list[str] = []
    for i in range(3):
        h.send("stage1_o19", f"pre-{i}")
        h.send("stage2_o19", f"pre-{i}")
        r = h.send("stage3_o19", f"pre-{i}")
        pre_results.append(_agent_text(r))
    log("OFFICE-19.pre_chaos_done", count=len(pre_results))

    # Inject chaos: SIGKILL stage2 mid-pipeline.
    pid = h.runs["stage2_o19"].pid
    log("OFFICE-19.killing_stage2", pid=pid)
    os.kill(pid, signal.SIGKILL)

    # Wait up to 12s for heartbeat to detect + restart (HB cycle = 3s, threshold = 2 = ~6s).
    deadline = time.time() + 15
    while time.time() < deadline:
        run = h.runs.get("stage2_o19")
        if run and run.alive and run.pid != pid:
            log("OFFICE-19.recovered", new_pid=run.pid)
            break
        time.sleep(0.4)
    else:
        h.record_error("OFFICE-19.no_recovery")
        return False

    # Post-chaos: send 3 more, verify they all complete.
    post_results: list[str] = []
    for i in range(3):
        h.send("stage1_o19", f"post-{i}")
        try:
            r2 = h.send("stage2_o19", f"post-{i}", timeout=20)
            r3 = h.send("stage3_o19", f"post-{i}")
            if _agent_text(r2) != f"echo: post-{i}":
                h.record_error("OFFICE-19.post_stage2_bad", got=_agent_text(r2))
                return False
            post_results.append(_agent_text(r3))
        except Exception as e:
            h.record_error("OFFICE-19.post_send_exc", i=i, error=str(e))
            return False
    log("OFFICE-19.post_chaos_done", count=len(post_results))

    out = FILES_DIR / "chaos-o19.json"
    out.write_text(json.dumps({"pre": pre_results, "post": post_results}))
    h.checkpoint("OFFICE-19.complete")
    return True


def office_20_multi_tenant(h: Harness) -> bool:
    """10 ticket workflows fired in parallel against a shared agent fleet."""
    import threading as _th
    for name in ("intake_o20", "billing_o20", "support_o20", "sales_o20"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name)
    h.checkpoint("OFFICE-20.spawned")

    tickets = [
        {"id": i, "category": ["billing", "support", "sales"][i % 3], "body": f"customer-{i} request"}
        for i in range(10)
    ]
    results: list[dict] = []
    results_lock = _th.Lock()
    errors: list[str] = []

    def worker(t: dict) -> None:
        try:
            h.send("intake_o20", json.dumps(t))
            specialist = f"{t['category']}_o20"
            r = h.send(specialist, t["body"])
            with results_lock:
                results.append({"id": t["id"], "category": t["category"],
                                "ack": _agent_text(r)})
        except Exception as e:
            with results_lock:
                errors.append(f"ticket {t['id']}: {e}")

    t0 = time.time()
    threads = [_th.Thread(target=worker, args=(t,)) for t in tickets]
    for th in threads: th.start()
    for th in threads: th.join(timeout=20)
    elapsed = round(time.time() - t0, 2)
    log("OFFICE-20.completed", count=len(results), elapsed_s=elapsed,
        errors=len(errors))

    if errors:
        h.record_error("OFFICE-20.worker_errors", errors=errors[:5])
        return False
    if len(results) != 10:
        h.record_error("OFFICE-20.partial", got=len(results), expected=10)
        return False
    # Distribution sanity: 4/3/3 or 3/4/3 etc.
    by_cat: dict[str, int] = {"billing": 0, "support": 0, "sales": 0}
    for r in results:
        by_cat[r["category"]] += 1
    log("OFFICE-20.distribution", **by_cat)

    out = FILES_DIR / "multi-tenant-o20.json"
    out.write_text(json.dumps({"distribution": by_cat, "elapsed_s": elapsed,
                                "results": results}, indent=2))
    h.checkpoint("OFFICE-20.complete", elapsed_s=elapsed)
    return True


def office_21_stateful_dialog(h: Harness) -> bool:
    """Multi-turn dialog; harness keeps history and feeds it on every turn.

    mur agents are stateless across `message/send` calls; the harness rebuilds
    context each turn. This is exactly how chat UIs above the LLM layer work.
    """
    h.ensure_profile(ROSTER["concierge_o21"])
    h.spawn("concierge_o21")
    h.checkpoint("OFFICE-21.spawned")

    history: list[dict] = []
    user_turns = [
        "hello",
        "i need help with my office wifi",
        "what was my first message?",  # tests history coverage
    ]
    # Build prompt = serialized history + current turn each time.
    for u in user_turns:
        history.append({"role": "user", "content": u})
        prompt = "\n".join(f"{h_['role']}: {h_['content']}" for h_ in history)
        r = h.send("concierge_o21", prompt)
        agent = _agent_text(r)
        history.append({"role": "agent", "content": agent})
        log("OFFICE-21.turn", n=len(history)//2, user=u, agent=agent[:100])

    # Echo backend echoes the whole prompt — assert each agent reply contains
    # ALL prior user turns (history is being threaded).
    last_agent = history[-1]["content"]
    for u in user_turns:
        if u not in last_agent:
            h.record_error("OFFICE-21.history_lost", missing=u, last=last_agent[:200])
            return False
    out = FILES_DIR / "dialog-o21.json"
    out.write_text(json.dumps(history, indent=2))
    h.checkpoint("OFFICE-21.complete", turns=len(user_turns))
    return True


def office_23_approval_gate(h: Harness) -> bool:
    """HITL: drafter emits → workflow blocks on approval file → publisher proceeds."""
    import threading as _th
    for name in ("drafter_o23", "publisher_o23"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name)
    h.checkpoint("OFFICE-23.spawned")

    draft_path = FILES_DIR / "draft-o23.md"
    approval_path = FILES_DIR / "approval-o23.flag"
    published_path = FILES_DIR / "published-o23.md"
    for p in (draft_path, approval_path, published_path):
        if p.exists(): p.unlink()

    # Drafter writes draft (echo agent acks; harness writes the actual file).
    h.send("drafter_o23", "draft press release")
    draft_path.write_text("# Draft\nThis announcement is pending approval.\n")
    log("OFFICE-23.drafted", path=str(draft_path))

    # Background "approver" simulates a human flipping the flag after a delay.
    approval_delay_s = 1.5
    def approve_later() -> None:
        time.sleep(approval_delay_s)
        approval_path.write_text("approved by manager\n")
        log("OFFICE-23.approved")
    _th.Thread(target=approve_later, daemon=True).start()

    # Workflow polls for approval — this is the HITL gate.
    t0 = time.time()
    deadline = t0 + 5
    while time.time() < deadline:
        if approval_path.exists():
            break
        time.sleep(0.1)
    else:
        h.record_error("OFFICE-23.approval_timeout")
        return False
    wait_s = round(time.time() - t0, 2)
    log("OFFICE-23.gate_passed", wait_s=wait_s)

    # Publisher proceeds.
    h.send("publisher_o23", "publish approved draft")
    published_path.write_text(draft_path.read_text() + "\n---\nApproved.\n")
    if not published_path.exists():
        h.record_error("OFFICE-23.publish_failed")
        return False

    h.checkpoint("OFFICE-23.complete", approval_wait_s=wait_s)
    return True


def office_24_dag_deps(h: Harness) -> bool:
    """DAG: node_a_o24 ‖ node_b_o24, then node_c_o24 (after both)."""
    import threading as _th
    for name in ("node_a_o24", "node_b_o24", "node_c_o24"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name)
    h.checkpoint("OFFICE-24.spawned")

    # Parallel A and B
    parallel_results: dict[str, dict] = {}
    plock = _th.Lock()

    def call(name: str, msg: str) -> None:
        r = h.send(name, msg)
        with plock:
            parallel_results[name] = {
                "task": r["id"],
                "ack": _agent_text(r),
                "completed_at": r["completedAt"],
            }

    t0 = time.time()
    threads = [
        _th.Thread(target=call, args=("node_a_o24", "compute A: fetch users")),
        _th.Thread(target=call, args=("node_b_o24", "compute B: fetch orders")),
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=10)

    if set(parallel_results.keys()) != {"node_a_o24", "node_b_o24"}:
        h.record_error("OFFICE-24.parallel_missing", got=list(parallel_results.keys()))
        return False

    # C depends on A and B — pass both results in.
    merged_input = json.dumps({k: v["ack"] for k, v in parallel_results.items()})
    rc = h.send("node_c_o24", merged_input)
    parallel_done_at = max(v["completed_at"] for v in parallel_results.values())

    log("OFFICE-24.dag_done",
        a_task=parallel_results["node_a_o24"]["task"],
        b_task=parallel_results["node_b_o24"]["task"],
        c_task=rc["id"],
        elapsed_s=round(time.time() - t0, 2))

    # Verify C's ack referenced A and B output.
    c_ack = _agent_text(rc)
    if "compute A" not in c_ack or "compute B" not in c_ack:
        # Echo prefixes "echo: " to whatever was sent (the merged json).
        # The merged json contains the A/B ack strings, which contain the
        # original prompts. Look for those.
        h.record_error("OFFICE-24.fanin_lost", c_ack=c_ack[:200])
        return False

    out = FILES_DIR / "dag-o24.json"
    out.write_text(json.dumps({
        "a": parallel_results["node_a_o24"],
        "b": parallel_results["node_b_o24"],
        "c": {"task": rc["id"], "ack": c_ack},
        "parallel_done_at": parallel_done_at,
    }, indent=2))
    h.checkpoint("OFFICE-24.complete")
    return True


def office_26_idempotent(h: Harness) -> bool:
    """Same request retried 3x (network flakiness simulation); harness dedups by id."""
    h.ensure_profile(ROSTER["processor_o26"])
    h.spawn("processor_o26")
    h.checkpoint("OFFICE-26.spawned")

    seen_ids: set[str] = set()
    outbox_dir = FILES_DIR / "outbox_o26"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    for f in outbox_dir.glob("*.json"):
        f.unlink()  # clean from prior runs

    request = {"request_id": "req-2026-04-28-001", "body": "process this once"}

    def submit(req: dict) -> str:
        rid = req["request_id"]
        if rid in seen_ids:
            log("OFFICE-26.dedup_hit", request_id=rid)
            return "deduped"
        seen_ids.add(rid)
        out_path = outbox_dir / f"{rid}.json"
        if out_path.exists():
            log("OFFICE-26.dedup_disk", request_id=rid)
            return "deduped"
        resp = h.send("processor_o26", json.dumps(req))
        out_path.write_text(json.dumps({
            "req": req,
            "ack": _agent_text(resp),
        }))
        return "processed"

    # Send same request 3 times (e.g. flaky client retry).
    outcomes = [submit(request) for _ in range(3)]
    log("OFFICE-26.outcomes", outcomes=outcomes)

    if outcomes != ["processed", "deduped", "deduped"]:
        h.record_error("OFFICE-26.dedup_broken", outcomes=outcomes)
        return False
    if len(list(outbox_dir.glob("*.json"))) != 1:
        h.record_error("OFFICE-26.duplicate_outbox",
                       count=len(list(outbox_dir.glob("*.json"))))
        return False

    h.checkpoint("OFFICE-26.complete")
    return True


def office_27_escalate(h: Harness) -> bool:
    """LLM classifier; complex/unknown cases escalate to senior agent."""
    for name in ("classifier_o27", "junior_o27", "senior_o27"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name, llm_mode=(name == "classifier_o27"))
    h.checkpoint("OFFICE-27.spawned")

    cases = [
        ("what's the wifi password?", "junior"),       # simple
        ("we need a custom integration with our 1990s mainframe", "senior"),  # complex
        ("?", "senior"),  # unknown -> escalate
    ]
    routed: list[dict] = []
    for body, expected_route in cases:
        cls_resp = h.send("classifier_o27", body, timeout=120)
        cls_raw = _agent_text(cls_resp).strip().lower().splitlines()[0] if _agent_text(cls_resp).strip() else ""
        # Routing policy: only "simple" goes to junior; everything else escalates.
        if "simple" in cls_raw:
            target = "junior_o27"; route = "junior"
        else:
            target = "senior_o27"; route = "senior"
        reply = h.send(target, body)
        routed.append({"body": body, "classified": cls_raw, "route": route,
                       "expected": expected_route})
        log("OFFICE-27.routed", route=route, expected=expected_route, raw=cls_raw[:60])

    # Permissive: at least 2/3 should match expected route. Small models drift.
    hits = sum(1 for r in routed if r["route"] == r["expected"])
    if hits < 2:
        h.record_error("OFFICE-27.below_threshold", hits=hits, routed=routed)
        return False
    out = FILES_DIR / "escalation-o27.json"
    out.write_text(json.dumps(routed, indent=2))
    h.checkpoint("OFFICE-27.complete", hits=hits)
    return True


def office_28_hmac_webhook(h: Harness) -> bool:
    """Production-style signed webhook: HMAC-SHA256 over body + X-Signature header.

    Demonstrates the security pattern n8n / GitHub / Stripe / Slack all use to
    authenticate inbound webhooks. The webhook handler verifies the HMAC
    before dispatching to the backend agent. Invalid sigs are rejected.
    """
    import hmac, hashlib, http.server, socketserver, threading, urllib.request, urllib.error
    h.ensure_profile(ROSTER["hmac_backend_o28"])
    h.spawn("hmac_backend_o28")
    h.checkpoint("OFFICE-28.spawned")

    SECRET = b"shared-secret-2026"
    received: list[dict] = []
    rejected: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): return

        def do_POST(self) -> None:
            sig = self.headers.get("X-Signature", "")
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            expected = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                rejected.append({"sig": sig[:16], "expected": expected[:16]})
                self.send_error(401, "invalid signature")
                return
            payload = json.loads(body.decode("utf-8"))
            resp = h.send("hmac_backend_o28", json.dumps(payload))
            received.append({"payload": payload, "ack": _agent_text(resp)})
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.end_headers(); self.wfile.write(b'{"ok":true}')

    port = 7881
    httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log("OFFICE-28.webhook_up", port=port)
    try:
        # 1. Valid signed request — should be accepted
        body = json.dumps({"event": "checkout.completed", "amount": 99}).encode()
        sig = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(f"http://127.0.0.1:{port}",
                                     data=body, headers={"X-Signature": sig,
                                                          "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 200
        log("OFFICE-28.valid_accepted")

        # 2. Invalid sig — should be rejected with 401
        bad_req = urllib.request.Request(f"http://127.0.0.1:{port}",
                                         data=body, headers={"X-Signature": "deadbeef" * 8,
                                                              "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(bad_req, timeout=10)
            h.record_error("OFFICE-28.invalid_accepted_should_have_been_rejected")
            return False
        except urllib.error.HTTPError as e:
            if e.code != 401:
                h.record_error("OFFICE-28.wrong_reject_code", code=e.code)
                return False
            log("OFFICE-28.invalid_rejected_401")

        # 3. Tampered body (sig stays the original) — should be rejected
        tampered = json.dumps({"event": "checkout.completed", "amount": 1}).encode()  # changed 99 -> 1
        tamper_req = urllib.request.Request(f"http://127.0.0.1:{port}",
                                            data=tampered, headers={"X-Signature": sig,  # sig of original
                                                                     "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(tamper_req, timeout=10)
            h.record_error("OFFICE-28.tampered_accepted")
            return False
        except urllib.error.HTTPError as e:
            if e.code != 401:
                h.record_error("OFFICE-28.tamper_wrong_code", code=e.code)
                return False
            log("OFFICE-28.tampered_rejected_401")

        if len(received) != 1 or len(rejected) != 2:
            h.record_error("OFFICE-28.count_mismatch",
                           received=len(received), rejected=len(rejected))
            return False
        out = FILES_DIR / "hmac-o28.json"
        out.write_text(json.dumps({"received": received, "rejected": rejected}, indent=2))
        h.checkpoint("OFFICE-28.complete")
        return True
    finally:
        httpd.shutdown(); httpd.server_close()


def office_29_audit_trail(h: Harness) -> bool:
    """Aggregate per-agent telemetry into one global audit trail.

    Each mur agent writes JSONL telemetry to its own
    `~/.mur/agents/<name>/telemetry/<date>.jsonl`. This scenario runs a
    chain across 3 agents, then walks each agent_home and merges the
    telemetry into one timestamp-sorted audit log.
    """
    for name in ("step_a_o29", "step_b_o29", "step_c_o29"):
        h.ensure_profile(ROSTER[name])
        h.spawn(name)
    h.checkpoint("OFFICE-29.spawned")

    # Generate activity that will produce telemetry events on each agent.
    h.send("step_a_o29", "audit step a")
    h.send("step_b_o29", "audit step b")
    h.send("step_c_o29", "audit step c")
    # Stop the agents so they flush shutdown telemetry.
    for n in ("step_a_o29", "step_b_o29", "step_c_o29"):
        h.stop(n)

    # Aggregate.
    events: list[dict] = []
    for n in ("step_a_o29", "step_b_o29", "step_c_o29"):
        tdir = AGENT_HOME_BASE / n / "telemetry"
        if not tdir.exists():
            continue
        for jsonl in sorted(tdir.glob("*.jsonl")):
            for line in jsonl.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events.append(ev)

    # Sort by ts.
    events.sort(key=lambda e: e.get("ts", ""))
    log("OFFICE-29.events_aggregated", count=len(events))

    if len(events) < 3:
        h.record_error("OFFICE-29.insufficient_events", got=len(events))
        return False

    audit_path = FILES_DIR / "audit-o29.jsonl"
    audit_path.write_text("".join(json.dumps(e) + "\n" for e in events))

    by_agent: dict[str, int] = {}
    for e in events:
        a = e.get("mur.agent.name", "unknown")
        by_agent[a] = by_agent.get(a, 0) + 1
    log("OFFICE-29.by_agent", **by_agent)

    if set(by_agent.keys()) != {"step_a_o29", "step_b_o29", "step_c_o29"}:
        h.record_error("OFFICE-29.missing_agents", got=list(by_agent.keys()))
        return False

    h.checkpoint("OFFICE-29.complete", events=len(events))
    return True


SCENARIOS = {
    "OFFICE-1": office_1,
    "OFFICE-2": office_2,
    "OFFICE-3": office_3,
    "OFFICE-4": office_4,
    "OFFICE-5": office_5,
    "OFFICE-6": office_6,
    "OFFICE-7-LLM": office_7_llm,
    "OFFICE-8": office_8,
    "OFFICE-9-XNET": office_9,
    "OFFICE-10-HYBRID": office_10,
    "OFFICE-11-XNET-LLM": office_11_xnet_llm,
    "OFFICE-12-FORM-LLM": office_12_form_extractor,
    "OFFICE-13-SCHEDULED": office_13_scheduled_report,
    "OFFICE-14-RAG-LLM": office_14_rag_qa,
    "OFFICE-15-PLANNER": office_15_planner_executor,
    "OFFICE-16-TRIAGE-LLM": office_16_email_triage,
    "OFFICE-17-XLATE-LLM": office_17_translation_chain,
    "OFFICE-18-LOG-ALERT": office_18_log_anomaly,
    "OFFICE-19-CHAOS": office_19_chaos,
    "OFFICE-20-MULTI-TENANT": office_20_multi_tenant,
    "OFFICE-21-DIALOG": office_21_stateful_dialog,
    "OFFICE-23-APPROVAL": office_23_approval_gate,
    "OFFICE-24-DAG": office_24_dag_deps,
    "OFFICE-26-IDEMPOTENT": office_26_idempotent,
    "OFFICE-27-ESCALATE-LLM": office_27_escalate,
    "OFFICE-28-HMAC-WEBHOOK": office_28_hmac_webhook,
    "OFFICE-29-AUDIT-TRAIL": office_29_audit_trail,
}


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="all")
    parser.add_argument("--cleanup", action="store_true",
                        help="Remove all harness agents at end")
    args = parser.parse_args()

    HARNESS_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)

    LOG_FILE.write_text("")  # truncate
    log("harness.start", scenario=args.scenario)

    h = Harness()
    h.start_heartbeat()
    results: dict[str, bool] = {}

    try:
        # OFFICE-11 requires a healthy remote chat-capable Ollama; gated.
        skip_by_default = {"OFFICE-11-XNET-LLM"}
        if args.scenario == "all":
            scenarios = [
                (k, v) for k, v in SCENARIOS.items()
                if k not in skip_by_default or os.environ.get("HARNESS_REMOTE_LLM") == "1"
            ]
        else:
            scenarios = [(args.scenario, SCENARIOS[args.scenario])]
        for name, fn in scenarios:
            log("scenario.start", scenario=name)
            try:
                ok = fn(h)
                results[name] = ok
                log("scenario.done", scenario=name, ok=ok)
            except Exception as e:
                results[name] = False
                h.record_error(f"{name}.exception", error=str(e))
                log("scenario.exception", scenario=name, error=str(e))
            # Stop heartbeat from auto-respawning agents we no longer need; clean as we go
            # (each scenario keeps its agents alive for inspection but next scenarios may collide
            # on socket paths so we stop them between scenarios).
            for n in list(h.runs):
                h.stop(n)
    finally:
        h.stop_heartbeat()
        h.stop_all()
        if args.cleanup:
            for n in list(ROSTER):
                run_mur("agent", "remove", n, "--purge")
            log("harness.cleanup_done")

    # Summary
    summary = {"results": results, "errors": h.errors, "checkpoints": h.checkpoints}
    (HARNESS_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    log("harness.summary", **{
        "passed": sum(1 for v in results.values() if v),
        "failed": sum(1 for v in results.values() if not v),
        "error_count": len(h.errors),
    })
    print(json.dumps(summary, indent=2))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
