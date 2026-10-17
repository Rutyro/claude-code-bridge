#!/usr/bin/env python3
"""cc-peer - cross-machine messaging between coding-agent sessions, over SSH.

Transport: `ssh <host> <command> -p [--resume <id>]`, message on stdin.
Stdlib only. MCP over stdio (newline-delimited JSON-RPC 2.0).

Config  ~/.cc-peer.json        (peers, per-peer settings)
State   ~/.cc-peer-state.json  (conversation id per peer)

Self-check:  python server.py --self-test
"""

from __future__ import annotations

import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import uuid

VERSION = "1.0.0"
PROTOCOL_FALLBACK = "2025-06-18"

CONFIG_PATH = pathlib.Path.home() / ".cc-peer.json"
STATE_PATH = pathlib.Path.home() / ".cc-peer-state.json"

CONFIG_TEMPLATE = {
    "peers": {
        "my-server": {
            "ssh": "my-server",
            "shell": "sh",
            "cwd": "",
            "command": "agent",
            "args": [],
            "ssh_args": ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"],
            "timeout": 600,
        }
    }
}

TOOLS = [
    {
        "name": "list_peers",
        "description": (
            "List the remote machines this session can message and whether each is "
            "reachable. Each peer runs its own coding-agent session on that machine."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "send_message",
        "description": (
            "Send a message to the coding-agent session running on another machine and "
            "return its reply. The remote session keeps its own conversation history "
            "across calls; it works on that machine against its own files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer": {
                    "type": "string",
                    "description": "Peer name as listed by list_peers.",
                },
                "message": {
                    "type": "string",
                    "description": "The message text to send.",
                },
                "new_session": {
                    "type": "boolean",
                    "description": (
                        "Start a fresh conversation on that machine instead of "
                        "continuing the existing one. Default false."
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": "Seconds to wait for the remote reply. Default: peer setting or 600.",
                },
            },
            "required": ["peer", "message"],
            "additionalProperties": False,
        },
    },
]


# --------------------------------------------------------------------------- config


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(CONFIG_TEMPLATE, indent=2), encoding="utf-8")
        raise FileNotFoundError(
            f"No config yet. Wrote a template to {CONFIG_PATH} - fill in your host, "
            f"then call list_peers again."
        )
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{CONFIG_PATH} is not valid JSON: {exc}") from exc


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def peer_or_raise(cfg: dict, name: str) -> dict:
    peers = cfg.get("peers") or {}
    if name not in peers:
        known = ", ".join(sorted(peers)) or "(none configured)"
        raise KeyError(f"Unknown peer '{name}'. Configured: {known}")
    peer = dict(peers[name])
    missing = [key for key in ("ssh", "command") if not peer.get(key)]
    if missing:
        raise KeyError(f"Peer '{name}' is missing: {', '.join(missing)}")
    return peer


# --------------------------------------------------------------------------- ssh


SSH_FALLBACKS = (
    r"C:\Program Files\Git\usr\bin\ssh.exe",
    os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "OpenSSH",
        "ssh.exe",
    ),
)


def _ssh_binary() -> str | None:
    """ssh from PATH, else well-known Windows locations (Git for Windows,
    Windows OpenSSH feature). An MCP server started from a plain Windows
    environment often has neither visible on PATH."""
    found = shutil.which("ssh")
    if found:
        return found
    for cand in SSH_FALLBACKS:
        if os.path.isfile(cand):
            return cand
    return None


def run_ssh(peer: dict, remote_cmd: str, stdin_text: str, timeout: int):
    """Run remote_cmd on the peer, feeding stdin_text to it as stdin.

    Returns (returncode, stdout, stderr). Never raises on a remote failure.
    """
    ssh_bin = _ssh_binary()
    if ssh_bin is None:
        return 127, "", "ssh not found on PATH."
    argv = [ssh_bin, "-T", *peer.get("ssh_args", []), peer["ssh"], remote_cmd]
    try:
        proc = subprocess.run(
            argv,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"Timed out after {timeout}s waiting for '{peer['ssh']}'."
    except FileNotFoundError:
        return 127, "", "ssh not found on PATH."
    return proc.returncode, proc.stdout, proc.stderr


def _cmd_quote(s: str) -> str:
    """cmd.exe quoting: double quotes, inner quotes doubled. Single quotes are
    literal in cmd, so shlex.quote would send them through as part of the arg."""
    return '"' + s.replace('"', '""') + '"'


def _quote(peer: dict):
    """Quoting function for this peer's remote shell."""
    return _cmd_quote if (peer.get("shell") or "sh").lower() == "cmd" else shlex.quote


def build_remote_cmd(peer: dict, session_id: str, resume: bool) -> str:
    """The remote shell command. session_id is always ours (uuid4) or a
    config value, so the only quoting that matters is cwd/command/args.

    peer["shell"] picks the dialect: "sh" (POSIX, shlex) or "cmd" (Windows
    OpenSSH default). Get it wrong and cmd receives literal single quotes.
    """
    shell = (peer.get("shell") or "sh").lower()
    q = _quote(peer)
    parts = []
    if peer.get("cwd"):
        parts.append(
            f"cd /d {_cmd_quote(peer['cwd'])} &&" if shell == "cmd"
            else f"cd {shlex.quote(peer['cwd'])} &&"
        )
    parts.append(q(peer["command"]))
    parts.append("-p")
    parts.append(
        f"--resume {q(session_id)}" if resume else f"--session-id {q(session_id)}"
    )
    parts.extend(q(a) for a in peer.get("args") or [])
    return " ".join(parts)


# Only these mean "the conversation is gone on that machine". A network blip must
# NOT be retried as a fresh session - that would silently drop the history.
STALE_SESSION = ("no conversation found", "session not found", "no such session",
                 "invalid session", "unknown session")


def send_to_peer(name: str, message: str, fresh: bool = False, timeout: int | None = None):
    """Send one message. Returns (ok, text, session_id)."""
    cfg = load_config()
    peer = peer_or_raise(cfg, name)
    wait = int(timeout or peer.get("timeout") or 600)

    state = load_state()
    previous = (state.get(name) or {}).get("session")

    if previous and not fresh:
        plan = [("resume", previous), ("new", str(uuid.uuid4()))]
    else:
        plan = [("new", str(uuid.uuid4()))]

    last_err = ""
    for i, (mode, sid) in enumerate(plan):
        code, out, err = run_ssh(peer, build_remote_cmd(peer, sid, mode == "resume"), message, wait)
        if code == 0:
            text = (out or "").strip()
            if mode == "new" and i > 0:
                text = f"({name}: previous conversation was gone, started a new one)\n{text}"
            state[name] = {"session": sid, "turns": (state.get(name) or {}).get("turns", 0) + 1}
            save_state(state)
            return True, text, sid

        last_err = err.strip() or out.strip() or f"exit {code}"
        if not (mode == "resume" and any(s in last_err.lower() for s in STALE_SESSION)):
            break

    return False, f"remote command on '{name}' failed.\n{last_err}", ""


def probe_peer(peer: dict) -> str:
    code, out, err = run_ssh(peer, f"{_quote(peer)(peer['command'])} --version", "", 20)
    if code == 0:
        return (out or "").strip().splitlines()[0] if out.strip() else "ok"
    first = (err or out).strip().splitlines()
    return "unreachable: " + (first[0] if first else f"exit {code}")


# --------------------------------------------------------------------------- tools


def tool_list_peers() -> str:
    try:
        cfg = load_config()
    except (FileNotFoundError, ValueError) as exc:
        return str(exc)
    peers = cfg.get("peers") or {}
    if not peers:
        return f"No peers configured in {CONFIG_PATH}."
    state = load_state()
    lines = []
    for name, peer in sorted(peers.items()):
        stored = (state.get(name) or {}).get("session")
        lines.append(
            f"{name}: host={peer['ssh']} shell={peer.get('shell') or 'sh'} | "
            f"{probe_peer(peer)} | conversation={'continuing' if stored else 'none yet'}"
        )
    return "\n".join(lines)


def tool_send_message(args: dict) -> tuple[str, bool]:
    peer = args.get("peer")
    message = args.get("message")
    if not peer or not message:
        return "Both 'peer' and 'message' are required.", True
    if not isinstance(message, str) or not message.strip():
        return "'message' must be a non-empty string.", True
    try:
        ok, text, sid = send_to_peer(
            peer,
            message,
            fresh=bool(args.get("new_session")),
            timeout=args.get("timeout"),
        )
    except (FileNotFoundError, ValueError, KeyError) as exc:
        return str(exc), True
    if ok:
        return text or "(the remote session returned nothing)", False
    return text, True


# --------------------------------------------------------------------------- mcp


def handle(req: dict):
    """One JSON-RPC request -> one response dict, or None for notifications."""
    method = req.get("method")
    rid = req.get("id")

    if method == "initialize":
        params = req.get("params") or {}
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_FALLBACK,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cc-peer", "version": VERSION},
            },
        }

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "list_peers":
            text, is_error = tool_list_peers(), False
        elif name == "send_message":
            text, is_error = tool_send_message(args)
        else:
            text, is_error = f"Unknown tool '{name}'.", True
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            },
        }

    if rid is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            resp = handle(req)
        except Exception as exc:  # never kill the server on one bad call
            resp = {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
            }
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


# --------------------------------------------------------------------------- self-test


def self_test() -> int:
    """Exercises the request/response contract with ssh stubbed out."""
    global run_ssh

    seen = {}

    def fake_ssh(peer, remote_cmd, stdin_text, timeout):
        seen["cmd"] = remote_cmd
        seen["stdin"] = stdin_text
        if remote_cmd.endswith("--version"):
            return 0, "2.1.299", ""
        if "--resume" in remote_cmd:
            return 0, "resumed reply", ""
        return 0, "fresh reply", ""

    real_ssh, run_ssh = run_ssh, fake_ssh
    tmp = pathlib.Path(os.environ.get("TEMP", "/tmp")) / "cc-peer-selftest"
    tmp.mkdir(exist_ok=True)
    global CONFIG_PATH, STATE_PATH
    real_cfg, real_state = CONFIG_PATH, STATE_PATH
    CONFIG_PATH, STATE_PATH = tmp / "cfg.json", tmp / "state.json"
    STATE_PATH.unlink(missing_ok=True)
    CONFIG_PATH.write_text(
        json.dumps({"peers": {"lab": {
            "ssh": "lab", "command": "agent", "cwd": "/srv/x", "args": ["--model", "m1"],
        }}}),
        encoding="utf-8",
    )

    try:
        assert handle({"method": "initialize", "id": 1, "params": {"protocolVersion": "2025-06-18"}})[
            "result"
        ]["serverInfo"]["name"] == "cc-peer"
        assert handle({"method": "notifications/initialized"}) is None
        assert len(handle({"method": "tools/list", "id": 2})["result"]["tools"]) == 2
        assert handle({"method": "nope", "id": 3})["error"]["code"] == -32601

        r = handle({"method": "tools/call", "id": 4, "params": {"name": "send_message",
                    "arguments": {"peer": "lab", "message": "hi"}}})
        assert r["result"]["isError"] is False, r
        assert r["result"]["content"][0]["text"] == "fresh reply"
        assert "--session-id" in seen["cmd"] and seen["stdin"] == "hi"
        assert "/srv/x" in seen["cmd"] and "--model m1" in seen["cmd"]

        first = json.loads(STATE_PATH.read_text(encoding="utf-8"))["lab"]["session"]

        r = handle({"method": "tools/call", "id": 5, "params": {"name": "send_message",
                    "arguments": {"peer": "lab", "message": "again"}}})
        assert r["result"]["content"][0]["text"] == "resumed reply", r
        assert f"--resume {first}" in seen["cmd"]

        r = handle({"method": "tools/call", "id": 6, "params": {"name": "send_message",
                    "arguments": {"peer": "ghost", "message": "x"}}})
        assert r["result"]["isError"] is True and "Unknown peer" in r["result"]["content"][0]["text"]

        # A message that looks like shell syntax must reach stdin verbatim.
        r = handle({"method": "tools/call", "id": 7, "params": {"name": "send_message",
                    "arguments": {"peer": "lab", "message": "rm -rf /; $(id)"}}})
        assert seen["stdin"] == "rm -rf /; $(id)" and r["result"]["isError"] is False
        assert "rm -rf" not in seen["cmd"]

        # Stale session id: attempt 1 fails, retry as fresh must succeed AND the
        # state must move to the new id (the bug a blind retry would leave behind).
        attempts = []

        def flaky(peer, remote_cmd, stdin_text, timeout):
            attempts.append(remote_cmd)
            if "--resume" in remote_cmd:
                return 1, "", "No conversation found with session ID"
            return 0, "recovered", ""

        run_ssh = flaky
        stale = first
        ok, text, new_id = send_to_peer("lab", "hello")
        assert ok and text.endswith("recovered"), (ok, text)
        assert "previous conversation was gone" in text, text
        assert len(attempts) == 2 and "--resume" in attempts[0] and "--session-id" in attempts[1]
        assert new_id and new_id != stale, new_id
        assert json.loads(STATE_PATH.read_text(encoding="utf-8"))["lab"]["session"] == new_id

        # A network failure must NOT silently start a new conversation.
        attempts.clear()

        def down(peer, remote_cmd, stdin_text, timeout):
            attempts.append(remote_cmd)
            return 255, "", "ssh: connect to host lab port 22: No route to host"

        run_ssh = down
        ok, text, _ = send_to_peer("lab", "hello")
        assert ok is False and len(attempts) == 1, (ok, attempts)
        assert json.loads(STATE_PATH.read_text(encoding="utf-8"))["lab"]["session"] == new_id

        # Windows peer: cmd.exe dialect - a POSIX build would send single quotes
        # through to cmd literally, which cmd does not strip.
        CONFIG_PATH.write_text(json.dumps({"peers": {"win": {
            "ssh": "win", "shell": "cmd", "cwd": r"C:\srv\my dir",
            "command": r"C:\Tools\agent\agent.exe",
            "args": ["--model", "m1"]}}}), encoding="utf-8")
        run_ssh = fake_ssh
        r = handle({"method": "tools/call", "id": 8, "params": {"name": "send_message",
                    "arguments": {"peer": "win", "message": "hi"}}})
        assert r["result"]["isError"] is False, r
        assert seen["cmd"].startswith('cd /d "C:\\srv\\my dir" && '), seen["cmd"]
        assert '"--model" "m1"' in seen["cmd"], seen["cmd"]
        assert "'" not in seen["cmd"], seen["cmd"]
        assert seen["stdin"] == "hi"
    finally:
        run_ssh, CONFIG_PATH, STATE_PATH = real_ssh, real_cfg, real_state

    print("self-test ok")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    serve()
