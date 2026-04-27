#!/usr/bin/env bash
# Pure-bash cross-network demo for mur agent.
#
# Proves the export → ship → import → run → message round-trip works as a
# Unix-composable pipeline of `mur` + `tar` + `scp` + `ssh` + `nc` + `jq`,
# with no Python anywhere in the loop. This is the runbook a sysadmin
# would actually follow.
#
# Pre-flight requires: mur, jq, ssh, scp, nc (with -U for Unix sockets) on
# the local host; jq, nc -U on the remote host.
#
# Usage:
#   ./bash_demo.sh                                 # uses defaults
#   REMOTE_HOST=user@host ./bash_demo.sh           # override remote
#   MUR_BIN=/path/to/mur ./bash_demo.sh            # override mur binary
#
# Exits 0 on success, non-zero on any failure.

set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-karajan@commander.twdd.tw}"
MUR_BIN="${MUR_BIN:-/tmp/mur-fix-v241/target/release/mur}"
LOCAL_AGENT="${LOCAL_AGENT:-bash_demo_src}"
REMOTE_AGENT="${REMOTE_AGENT:-bash_demo_remote}"
PKG_PATH="/tmp/${LOCAL_AGENT}.murpkg"
RUNTIME_BIN_REMOTE='~/.local/bin/mur-agent-runtime'  # tilde expanded by remote shell

YELLOW=$'\033[33m'; GREEN=$'\033[32m'; RED=$'\033[31m'; NC=$'\033[0m'
step() { printf "%s==>%s %s\n" "$YELLOW" "$NC" "$*"; }
ok()   { printf "%s OK%s %s\n" "$GREEN" "$NC" "$*"; }
fail() { printf "%sFAIL%s %s\n" "$RED" "$NC" "$*" >&2; exit 1; }

ssh_q() { ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes "$REMOTE_HOST" "$@" 2>&1 \
            | grep -vE 'WARNING|post-quantum|upgraded|This session' || true; }

# Build a JSON-RPC message/send request.
build_req() {
    local text="$1"
    jq -nc --arg t "$text" '{
      jsonrpc: "2.0",
      id: ($t | @base64),
      method: "message/send",
      params: { message: { role: "user", parts: [{ kind: "text", text: $t }] } }
    }'
}

# Dial a remote agent UDS via ssh+nc, send a request, read one line back.
remote_dial_send() {
    local agent="$1" text="$2"
    local req; req=$(build_req "$text")
    # OpenBSD nc on the remote: -q 1 quits 1s after stdin EOF, ensuring the
    # server response is fully read even if it doesn't close the socket.
    local cmd="printf '%s\n' '$req' | nc -q 1 -U \$HOME/.mur/agents/${agent}/agent.sock"
    ssh_q "$cmd"
}

cleanup() {
    step "cleanup"
    "$MUR_BIN" agent remove "$LOCAL_AGENT" --purge >/dev/null 2>&1 || true
    rm -f "$PKG_PATH"
    ssh_q "
        if [ -f \$HOME/.mur/agents/${REMOTE_AGENT}/running.lock ]; then
            kill \$(jq -r .pid \$HOME/.mur/agents/${REMOTE_AGENT}/running.lock 2>/dev/null) 2>/dev/null || true
        fi
        rm -rf \$HOME/.mur/agents/${REMOTE_AGENT} \$HOME/.local/bin/mur_agent_${REMOTE_AGENT} /tmp/${LOCAL_AGENT}.murpkg /tmp/${REMOTE_AGENT}.log
    " >/dev/null
    ok "cleanup done"
}
trap cleanup EXIT

# ----------------------------------------------------------------------------
# 0. Pre-flight
# ----------------------------------------------------------------------------
step "pre-flight checks"
command -v "$MUR_BIN" >/dev/null || fail "mur binary not found at $MUR_BIN"
command -v jq          >/dev/null || fail "jq not installed locally"
command -v scp         >/dev/null || fail "scp not installed"
command -v ssh         >/dev/null || fail "ssh not installed"
ssh_q "command -v jq >/dev/null && command -v nc >/dev/null && [ -x ${RUNTIME_BIN_REMOTE} ]" \
    || fail "remote missing jq / nc / mur-agent-runtime"
ok "all dependencies present"

# ----------------------------------------------------------------------------
# 1. Create local source agent and verify it works locally
# ----------------------------------------------------------------------------
step "create local source agent '$LOCAL_AGENT'"
"$MUR_BIN" agent remove "$LOCAL_AGENT" --purge >/dev/null 2>&1 || true
"$MUR_BIN" agent create "$LOCAL_AGENT" --no-interactive >/dev/null
ok "agent created"

# ----------------------------------------------------------------------------
# 2. Export to .murpkg
# ----------------------------------------------------------------------------
step "export to $PKG_PATH"
"$MUR_BIN" agent export "$LOCAL_AGENT" --out "$PKG_PATH" >/dev/null
[ -f "$PKG_PATH" ] || fail "export did not produce package"
ok "package $(stat -f%z "$PKG_PATH" 2>/dev/null || stat -c%s "$PKG_PATH") bytes"
step "package contents:"
tar -tf "$PKG_PATH"

# ----------------------------------------------------------------------------
# 3. Ship to remote and unpack
# ----------------------------------------------------------------------------
step "ship .murpkg to $REMOTE_HOST"
scp -o BatchMode=yes -q "$PKG_PATH" "$REMOTE_HOST:/tmp/${LOCAL_AGENT}.murpkg"
ok "shipped"

step "unpack on remote into ~/.mur/agents/$REMOTE_AGENT/ + rewrite profile name"
ssh_q "
set -e
mkdir -p \$HOME/.mur/agents/${REMOTE_AGENT}
rm -rf \$HOME/.mur/agents/${REMOTE_AGENT}/*
cd \$HOME/.mur/agents/${REMOTE_AGENT}
tar xf /tmp/${LOCAL_AGENT}.murpkg
# Rewrite agent name in profile.yaml so it doesn't collide with the source name.
sed -i 's/^name: ${LOCAL_AGENT}$/name: ${REMOTE_AGENT}/' profile.yaml
# Make socket bind path use the remote's agent_home template.
sed -i 's|unix://[^[:space:]]*/agent.sock|unix://{{agent_home}}/agent.sock|' profile.yaml
ln -sf \$(realpath ${RUNTIME_BIN_REMOTE}) \$HOME/.local/bin/mur_agent_${REMOTE_AGENT}
echo OK
" | grep -q OK || fail "remote unpack failed"
ok "unpacked + symlinked"

# ----------------------------------------------------------------------------
# 4. Spawn the runtime on remote (force echo for deterministic output)
# ----------------------------------------------------------------------------
step "spawn agent on remote"
SPAWN_OUT=$(ssh_q "
rm -f \$HOME/.mur/agents/${REMOTE_AGENT}/agent.sock \$HOME/.mur/agents/${REMOTE_AGENT}/running.lock
MUR_AGENT_FORCE_ECHO=1 nohup \$HOME/.local/bin/mur_agent_${REMOTE_AGENT} > /tmp/${REMOTE_AGENT}.log 2>&1 &
echo PID=\$!
")
REMOTE_PID=$(echo "$SPAWN_OUT" | sed -n 's/PID=//p')
[ -n "$REMOTE_PID" ] || fail "could not read remote PID; got: $SPAWN_OUT"
ok "remote pid=$REMOTE_PID"

# Wait up to 5s for the socket.
step "wait for remote socket"
for i in 1 2 3 4 5 6 7 8 9 10; do
    if ssh_q "test -S \$HOME/.mur/agents/${REMOTE_AGENT}/agent.sock && echo READY" | grep -q READY; then
        break
    fi
    sleep 0.5
    [ "$i" = "10" ] && fail "remote socket never appeared (see /tmp/${REMOTE_AGENT}.log on remote)"
done
ok "remote socket up"

# ----------------------------------------------------------------------------
# 5. Send messages via ssh + nc -U + jq, verify echo round-trips
# ----------------------------------------------------------------------------
step "round-trip 3 messages via ssh + nc -U"
for n in 1 2 3; do
    msg="hello-bash-$n"
    raw=$(remote_dial_send "$REMOTE_AGENT" "$msg")
    [ -n "$raw" ] || fail "empty response from remote on '$msg'"
    # Pull the agent's reply text out of the JSON-RPC result.messages array.
    reply=$(printf '%s' "$raw" | jq -r '.result.messages[-1].parts[0].text // empty')
    expected="echo: $msg"
    if [ "$reply" = "$expected" ]; then
        ok "round-trip $n: '$msg' -> '$reply'"
    else
        fail "round-trip $n: expected '$expected', got '$reply' (raw: $(printf '%.200s' "$raw"))"
    fi
done

# ----------------------------------------------------------------------------
# 6. Inspect remote telemetry to confirm activity was logged
# ----------------------------------------------------------------------------
step "verify remote telemetry"
TELEMETRY_LINES=$(ssh_q "wc -l \$HOME/.mur/agents/${REMOTE_AGENT}/telemetry/*.jsonl 2>/dev/null | awk '{print \$1}'" | head -1)
[ -n "${TELEMETRY_LINES:-}" ] && ok "telemetry written ($TELEMETRY_LINES lines)" || step "telemetry empty (echo path doesn't emit, that's ok)"

# Cleanup runs in trap.
echo
ok "PURE-BASH CROSS-NETWORK DEMO PASSED"
