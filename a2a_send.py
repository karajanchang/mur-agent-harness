#!/usr/bin/env python3
"""Tiny A2A JSON-RPC client for mur agents over Unix Domain Socket.

Used on the remote host where the full `mur` CLI isn't installed — only
`mur-agent-runtime`. The agent's UDS is at:
    $HOME/.mur/agents/<name>/agent.sock

Usage:
    a2a_send.py <agent_name> '<message_json>'
where message_json is the A2A `message/send` payload, e.g.:
    {"role":"user","parts":[{"kind":"text","text":"hello"}]}

Prints the JSON-RPC `result` field to stdout.
Exit codes: 0 ok, 1 transport error, 2 protocol error.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: a2a_send.py <agent_name> <message_json>", file=sys.stderr)
        return 2
    name, msg_json = sys.argv[1], sys.argv[2]
    try:
        msg = json.loads(msg_json)
    except json.JSONDecodeError as e:
        print(f"invalid message json: {e}", file=sys.stderr)
        return 2

    sock_path = Path.home() / ".mur" / "agents" / name / "agent.sock"
    if not sock_path.exists():
        print(f"socket missing: {sock_path}", file=sys.stderr)
        return 1

    req = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {"message": msg},
    }
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(sock_path))
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        # Read until we have a complete JSON object (newline-delimited).
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        s.close()
    except OSError as e:
        print(f"transport error: {e}", file=sys.stderr)
        return 1

    line = buf.split(b"\n", 1)[0]
    try:
        resp = json.loads(line)
    except json.JSONDecodeError as e:
        print(f"bad response (not json): {e}\nraw: {line!r}", file=sys.stderr)
        return 2
    if "error" in resp:
        print(json.dumps(resp["error"]), file=sys.stderr)
        return 2
    print(json.dumps(resp.get("result", {})))
    return 0


if __name__ == "__main__":
    sys.exit(main())
