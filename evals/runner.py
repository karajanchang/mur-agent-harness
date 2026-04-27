#!/usr/bin/env python3
"""
mur agent eval runner — Python prototype for `mur eval`.

Reads an `agent.eval.yaml` describing a test suite and a provider matrix,
spins up an ephemeral mur agent for each (provider × system_prompt) cell,
runs every test case, scores responses, and writes a markdown + JSONL
report.

Designed to be portable to Rust later; the YAML schema is the contract.

Usage:
    python3 evals/runner.py <suite.yaml>
    python3 evals/runner.py <suite.yaml> --json-out results.json
    python3 evals/runner.py <suite.yaml> --report-md report.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

try:
    import yaml  # PyYAML — optional, used when suite is *.yaml/*.yml
    _HAVE_YAML = True
except ImportError:
    _HAVE_YAML = False

# Reuse harness constants where available.
HARNESS_DIR = Path("/tmp/mur-agent-harness")
EVALS_DIR = HARNESS_DIR / "evals_runs"
EVALS_DIR.mkdir(parents=True, exist_ok=True)

MUR = os.environ.get("HARNESS_MUR_BIN", "/tmp/mur-fix-v241/target/release/mur")
RUNTIME_DIR = Path.home() / ".local" / "bin"
AGENT_HOME_BASE = Path.home() / ".mur" / "agents"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    provider: str       # e.g. "ollama/llama3.2:3b" or "echo" (synthetic)
    system_prompt: str  # path to .md file (or "@default" to use agent's own)


@dataclass
class TestCase:
    id: str
    input: str
    expects: list[dict[str, Any]]


@dataclass
class CheckResult:
    kind: str
    passed: bool
    detail: str = ""


@dataclass
class CellTestResult:
    cell: dict[str, str]
    test_id: str
    input: str
    response: str
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    latency_ms: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# YAML schema → typed objects
# ---------------------------------------------------------------------------


def parse_suite(path: Path) -> dict[str, Any]:
    text = path.read_text()
    if path.suffix in (".json",):
        raw = json.loads(text)
    elif path.suffix in (".yaml", ".yml"):
        if not _HAVE_YAML:
            raise SystemExit(
                "PyYAML not installed but suite is YAML. Either `pip install pyyaml` "
                "(use --break-system-packages on macOS python.org), or convert the "
                "suite to JSON (same schema)."
            )
        raw = yaml.safe_load(text)
    else:
        # Try YAML first if available, then JSON.
        if _HAVE_YAML:
            try:
                raw = yaml.safe_load(text)
            except Exception:
                raw = json.loads(text)
        else:
            raw = json.loads(text)
    if "name" not in raw or "test_cases" not in raw:
        raise ValueError("suite must have 'name' and 'test_cases'")
    return raw


def expand_matrix(matrix: dict | None, default_prompt: str) -> list[Cell]:
    if not matrix:
        return [Cell(provider="echo", system_prompt=default_prompt)]
    providers = matrix.get("providers") or ["echo"]
    sys_prompts = matrix.get("system_prompts") or [default_prompt]
    cells: list[Cell] = []
    for p in providers:
        for sp in sys_prompts:
            cells.append(Cell(provider=p, system_prompt=sp))
    return cells


# ---------------------------------------------------------------------------
# Scorers
# ---------------------------------------------------------------------------


def _maybe_lower(s: str, case: str | None) -> str:
    return s.lower() if (case == "insensitive") else s


def score_check(check: dict, response: str, *, llm_judge_fn=None) -> CheckResult:
    kind = check.get("kind")
    case = check.get("case")
    r = _maybe_lower(response, case)

    if kind == "contains":
        needles = [_maybe_lower(s, case) for s in check.get("any_of", [])]
        hit = any(n in r for n in needles)
        return CheckResult(kind, hit, f"any_of={needles!r} hit={hit}")

    if kind == "not_contains":
        needles = [_maybe_lower(s, case) for s in check.get("any_of", [])]
        hit = any(n in r for n in needles)
        return CheckResult(kind, not hit, f"forbidden={needles!r} found={hit}")

    if kind == "equals":
        target = _maybe_lower(check.get("value", ""), case)
        return CheckResult(kind, r.strip() == target,
                           f"expected={target!r} got={r.strip()[:80]!r}")

    if kind == "regex":
        pat = check.get("pattern", "")
        flags = re.IGNORECASE if case == "insensitive" else 0
        m = re.search(pat, response, flags)
        return CheckResult(kind, m is not None, f"pattern={pat!r}")

    if kind == "length_min":
        n = int(check.get("value", 0))
        return CheckResult(kind, len(response) >= n, f"len={len(response)} need>={n}")

    if kind == "length_max":
        n = int(check.get("value", 1 << 30))
        return CheckResult(kind, len(response) <= n, f"len={len(response)} need<={n}")

    if kind == "json_shape":
        try:
            obj = json.loads(response.strip())
        except Exception:
            # Try to find a JSON object inside the response.
            m = re.search(r"\{.*\}", response, re.DOTALL)
            if not m:
                return CheckResult(kind, False, "no JSON found")
            try:
                obj = json.loads(m.group(0))
            except Exception:
                return CheckResult(kind, False, "JSON parse failed")
        keys = check.get("keys_required", [])
        missing = [k for k in keys if k not in obj]
        return CheckResult(kind, not missing,
                           f"required={keys} missing={missing}")

    if kind == "llm_judge":
        if llm_judge_fn is None:
            return CheckResult(kind, False, "no judge callable provided")
        rubric = check.get("rubric", "Is the reply good?")
        threshold = float(check.get("threshold", 0.7))
        score = llm_judge_fn(rubric, response)
        return CheckResult(kind, score >= threshold,
                           f"score={score:.2f} threshold={threshold:.2f}")

    return CheckResult(kind or "unknown", False, "unknown check kind")


# ---------------------------------------------------------------------------
# mur agent runner — ephemeral agent per cell
# ---------------------------------------------------------------------------


def run_mur(*args: str, env: dict | None = None,
            timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [MUR, *args],
        env={**os.environ, **(env or {})},
        capture_output=True, text=True, timeout=timeout,
    )


def _ensure_ephemeral_agent(name: str, provider: str, sys_prompt_text: str) -> None:
    """Create an agent profile parameterised by provider + system_prompt."""
    # Best-effort cleanup if a prior run left it behind.
    run_mur("agent", "remove", name, "--purge", timeout=10)

    if provider == "echo":
        # No real model; harness will run agent in FORCE_ECHO mode.
        out = run_mur("agent", "create", name, "--no-interactive", timeout=15)
    else:
        # Provider matrix entry like "ollama/llama3.2:3b" or "anthropic/claude-..."
        out = run_mur("agent", "create", name, "--no-interactive",
                      "--model", provider, timeout=15)
    if out.returncode != 0:
        raise RuntimeError(f"create {name}: {out.stderr.strip()}")

    # Set system prompt via mur CLI (positional arg).
    out = run_mur("agent", "prompt", "set", name, sys_prompt_text, timeout=10)
    if out.returncode != 0:
        raise RuntimeError(f"prompt set {name}: {out.stderr.strip()}")


def _load_sys_prompt(spec: str, agent_default_path: Path) -> str:
    """Resolve `@sys_prompt.md` style references; literal text otherwise."""
    if spec == "@default":
        return agent_default_path.read_text() if agent_default_path.exists() else ""
    if spec.startswith("@"):
        p = Path(spec[1:])
        if not p.is_absolute():
            p = agent_default_path.parent / p
        return p.read_text()
    return spec


def _spawn_and_wait(name: str, llm_mode: bool) -> subprocess.Popen:
    sym = RUNTIME_DIR / f"mur_agent_{name}"
    sock = AGENT_HOME_BASE / name / "agent.sock"
    if sock.exists():
        sock.unlink()
    env = {**os.environ}
    if not llm_mode:
        env["MUR_AGENT_FORCE_ECHO"] = "1"
    proc = subprocess.Popen([str(sym)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.time() + 8
    while time.time() < deadline:
        if sock.exists():
            return proc
        if proc.poll() is not None:
            err = (proc.stderr.read().decode("utf-8", errors="replace")
                   if proc.stderr else "")
            raise RuntimeError(f"agent {name} died: {err[:300]}")
        time.sleep(0.1)
    proc.terminate()
    raise RuntimeError(f"agent {name} socket never appeared")


def _send(name: str, text: str, timeout: float = 90) -> tuple[str, int]:
    """Returns (agent_text, latency_ms)."""
    msg = json.dumps({"role": "user", "parts": [{"kind": "text", "text": text}]})
    t0 = time.time()
    out = run_mur("agent", "send", name, msg, timeout=timeout)
    latency = int((time.time() - t0) * 1000)
    if out.returncode != 0:
        raise RuntimeError(f"send {name}: {out.stderr.strip()}")
    resp = json.loads(out.stdout)
    text_out = next((p["text"] for m in resp["messages"]
                     if m["role"] == "agent" for p in m["parts"]), "")
    return text_out, latency


def _stop(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------------
# Core run loop
# ---------------------------------------------------------------------------


def make_llm_judge(judge_provider: str):
    """Return a callable(rubric, response) -> float in [0,1] using a judge agent.

    For prototype simplicity we ask the judge to reply with a single integer
    0..10 and divide by 10. Robust enough for harness use.
    """
    judge_name = f"eval_judge_{uuid.uuid4().hex[:8]}"
    sys_prompt = (
        "You are a strict evaluator. Given a RUBRIC and a CANDIDATE reply, "
        "judge how well the candidate satisfies the rubric. Reply with ONLY "
        "a single integer from 0 to 10, where 10 is perfect and 0 is terrible. "
        "No preamble, no explanation, no punctuation — JUST the number."
    )
    _ensure_ephemeral_agent(judge_name, judge_provider, sys_prompt)
    proc = _spawn_and_wait(judge_name, llm_mode=True)

    def judge(rubric: str, candidate: str) -> float:
        prompt = f"RUBRIC: {rubric}\n\nCANDIDATE: {candidate}\n\nScore (0-10):"
        try:
            text, _ = _send(judge_name, prompt, timeout=120)
        except Exception:
            return 0.0
        m = re.search(r"\b(10|[0-9])\b", text)
        if not m:
            return 0.0
        return min(10, max(0, int(m.group(1)))) / 10.0

    judge.cleanup = lambda: (_stop(proc),
                             run_mur("agent", "remove", judge_name, "--purge"))
    return judge


def run_suite(suite_path: Path) -> dict:
    suite = parse_suite(suite_path)
    base_agent = suite.get("agent")  # informational; we always use ephemeral
    cells = expand_matrix(suite.get("matrix"),
                          default_prompt="@default")
    test_cases = [TestCase(id=tc["id"], input=tc["input"], expects=tc["expects"])
                  for tc in suite["test_cases"]]

    run_id = f"eval-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    print(f"[runner] suite={suite['name']} run_id={run_id} cells={len(cells)} cases={len(test_cases)}")

    # If any check is llm_judge, instantiate one judge agent for the run.
    judge = None
    needs_judge = any(c.get("kind") == "llm_judge"
                      for tc in test_cases for c in tc.expects)
    if needs_judge:
        judge_provider = next(
            (c["judge"] for tc in test_cases for c in tc.expects
             if c.get("kind") == "llm_judge" and c.get("judge")),
            "ollama/llama3.2:3b",
        )
        print(f"[runner] llm_judge enabled, provider={judge_provider}")
        judge = make_llm_judge(judge_provider)

    results: list[CellTestResult] = []
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    try:
        for cell in cells:
            agent_name = f"eval_{run_id[-6:]}_{abs(hash((cell.provider, cell.system_prompt))) % 0xffff:04x}"
            sys_prompt_text = _load_sys_prompt(
                cell.system_prompt,
                AGENT_HOME_BASE / (base_agent or "_") / "sys_prompt.md",
            ) if cell.system_prompt != "@default" else "You are a helpful assistant."

            print(f"[runner] cell provider={cell.provider} prompt={cell.system_prompt[:30]!r} → {agent_name}")
            llm_mode = (cell.provider != "echo")
            try:
                _ensure_ephemeral_agent(agent_name, cell.provider, sys_prompt_text)
                proc = _spawn_and_wait(agent_name, llm_mode=llm_mode)
            except Exception as e:
                # Failure to spawn — record one synthetic result per case, move on.
                for tc in test_cases:
                    results.append(CellTestResult(
                        cell=asdict(cell), test_id=tc.id, input=tc.input,
                        response="", passed=False, error=f"spawn: {e}",
                    ))
                continue

            try:
                for tc in test_cases:
                    try:
                        text, latency = _send(agent_name, tc.input)
                    except Exception as e:
                        results.append(CellTestResult(
                            cell=asdict(cell), test_id=tc.id, input=tc.input,
                            response="", passed=False, error=str(e),
                        ))
                        continue

                    checks = [score_check(c, text, llm_judge_fn=judge)
                              for c in tc.expects]
                    all_passed = all(c.passed for c in checks)
                    results.append(CellTestResult(
                        cell=asdict(cell), test_id=tc.id, input=tc.input,
                        response=text, passed=all_passed,
                        checks=checks, latency_ms=latency,
                    ))
                    print(f"  {tc.id} → {'PASS' if all_passed else 'FAIL'} ({latency}ms)")
            finally:
                _stop(proc)
                run_mur("agent", "remove", agent_name, "--purge")
    finally:
        if judge is not None:
            judge.cleanup()

    finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    summary = _summarise(results, cells)
    return {
        "run_id": run_id,
        "suite": suite["name"],
        "agent": base_agent,
        "started_at": started_at,
        "finished_at": finished_at,
        "matrix": {
            "providers": list({c.provider for c in cells}),
            "system_prompts": list({c.system_prompt for c in cells}),
        },
        "results": [{**asdict(r),
                     "checks": [asdict(c) for c in r.checks]}
                    for r in results],
        "summary": summary,
    }


def _summarise(results: list[CellTestResult], cells: list[Cell]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    by_provider: dict[str, dict[str, int]] = {}
    for r in results:
        prov = r.cell["provider"]
        d = by_provider.setdefault(prov, {"pass": 0, "fail": 0})
        d["pass" if r.passed else "fail"] += 1
    return {
        "total": total, "passed": passed, "failed": total - passed,
        "by_provider": by_provider,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_markdown(report: dict) -> str:
    lines = [f"# Eval report — {report['suite']}",
             f"**run_id:** `{report['run_id']}`",
             f"**started:** {report['started_at']} → {report['finished_at']}",
             ""]

    # Summary table
    lines.append("## Summary")
    lines.append(f"- **Total**: {report['summary']['total']}")
    lines.append(f"- **Passed**: {report['summary']['passed']}")
    lines.append(f"- **Failed**: {report['summary']['failed']}")
    lines.append("")
    lines.append("| Provider | Pass | Fail |")
    lines.append("|----------|------|------|")
    for prov, d in sorted(report["summary"]["by_provider"].items()):
        lines.append(f"| {prov} | {d['pass']} | {d['fail']} |")
    lines.append("")

    # Side-by-side matrix view
    providers = sorted({r["cell"]["provider"] for r in report["results"]})
    test_ids = sorted({r["test_id"] for r in report["results"]})
    lines.append("## Matrix (per test, per provider)")
    lines.append("| test_id | " + " | ".join(providers) + " |")
    lines.append("|" + "---|" * (len(providers) + 1))
    for tid in test_ids:
        row = [tid]
        for prov in providers:
            r = next((r for r in report["results"]
                      if r["test_id"] == tid and r["cell"]["provider"] == prov), None)
            if not r:
                row.append("—")
            elif r["passed"]:
                row.append(f"✅ {r['latency_ms']}ms")
            else:
                err = r.get("error") or _first_failed_check(r["checks"])
                row.append(f"❌ {err}"[:60])
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Detail per failure
    failures = [r for r in report["results"] if not r["passed"]]
    if failures:
        lines.append("## Failures")
        for r in failures:
            lines.append(f"### `{r['test_id']}` on `{r['cell']['provider']}`")
            lines.append(f"**input:** `{r['input']}`")
            if r.get("error"):
                lines.append(f"**error:** {r['error']}")
            lines.append(f"**response:**")
            lines.append("```")
            lines.append(r["response"][:500])
            lines.append("```")
            for c in r["checks"]:
                if not c["passed"]:
                    lines.append(f"- ❌ `{c['kind']}`: {c['detail']}")
            lines.append("")
    return "\n".join(lines)


def _first_failed_check(checks: list[dict]) -> str:
    for c in checks:
        if not c["passed"]:
            return f"{c['kind']}: {c['detail']}"
    return "fail"


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("suite", help="path to agent.eval.yaml")
    p.add_argument("--json-out", default=None, help="write full results JSON here")
    p.add_argument("--report-md", default=None, help="write markdown report here")
    args = p.parse_args()

    suite_path = Path(args.suite).resolve()
    report = run_suite(suite_path)

    json_out = Path(args.json_out) if args.json_out else \
        EVALS_DIR / f"{report['run_id']}.json"
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(report, indent=2))
    print(f"[runner] json -> {json_out}")

    md_out = Path(args.report_md) if args.report_md else \
        EVALS_DIR / f"{report['run_id']}.md"
    md_out.write_text(render_markdown(report))
    print(f"[runner] md   -> {md_out}")

    s = report["summary"]
    print(f"[runner] {s['passed']}/{s['total']} passed")
    return 0 if s["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
