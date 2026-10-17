# claude-code-bridge

A small MCP server that lets one coding-agent session message another machine's session over plain SSH, and get the reply back. No cloud relay, no shared account, no extra API keys.

The internal name is `cc-peer`; this repository is its public home. It is published on PyPI as `claude-code-bridge-mcp`.

```
 ┌────────────────────┐      stdio (MCP)      ┌────────────┐      ssh      ┌──────────────────┐
 │  agent (host)      │ ◄───────────────────► │  cc_peer.py│ ────────────► │  agent -p        │
 │  list_peers /      │                       │  (local)   │ [--resume]    │  (remote machine)│
 │  send_message      │                       └────────────┘               └──────────────────┘
 └────────────────────┘
```

## Why

Cross-session messaging between machines is often tied to a cloud account or a shared login. SSH is usually already set up and needs neither. So this server just uses SSH.

Each peer keeps its own conversation history: the bridge stores the remote session id and resumes it on the next message. It only starts a fresh session when the old one is provably gone. A network error never silently drops the history.

## Install

```bash
pipx install claude-code-bridge-mcp==1.0.0
```

or run it without installing:

```bash
uvx --from claude-code-bridge-mcp==1.0.0 cc-peer
```

Pin the version in client configs and bump it deliberately. To audit exactly what you run, install from source: `python cc_peer.py`.

## Features

- MCP over stdio - works with any MCP-capable client. Single file, Python standard library only.
- `list_peers` - shows the configured peers and whether each is reachable.
- `send_message` - sends a message, returns the reply, and keeps the conversation going across calls.
- Session resume with stale-session detection and safe retry.
- Windows-aware - correct quoting for both POSIX (`sh`) and Windows (`cmd`) peers.
- Built-in self-test: `cc-peer --self-test`.

## Quickstart

Requirements: Python 3.11+, the same agent CLI on both machines, and SSH key auth to the peer (`BatchMode`-friendly).

1. Create `~/.cc-peer.json` (or start from `examples/cc-peer.example.json`):

```json
{
  "peers": {
    "my-server": {
      "ssh": "my-server",
      "shell": "sh",
      "cwd": "",
      "command": "agent",
      "args": [],
      "ssh_args": ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"],
      "timeout": 600
    }
  }
}
```

- `ssh` - a `~/.ssh/config` host (recommended) or `user@host`.
- `shell` - `sh` for Linux/macOS peers, `cmd` for Windows peers.
- `cwd` - optional: run the remote command from a different directory.
- `command` - the remote CLI to run. It must accept `-p`, `--session-id <id>` and `--resume <id>`.
- `args` - optional extra arguments for that command.

2. Register the server with your MCP client. With `uvx`:

```json
{
  "mcpServers": {
    "cc-peer": {
      "command": "uvx",
      "args": ["--from", "claude-code-bridge-mcp==1.0.0", "cc-peer"]
    }
  }
}
```

After `pipx install`, use `"command": "cc-peer"` with no args instead.

3. Try it from the client:

```
list_peers()
send_message(peer="my-server", message="What is in your working directory?")
```

Session ids are stored in `~/.cc-peer-state.json` - never commit it.

## How it works

`send_message` runs `ssh <peer> <command> -p --resume <session-id>` and pipes the message to stdin. The remote session's stdout is the reply. Session ids are kept per peer in a small state file; if the remote reports that the conversation no longer exists, the bridge retries once with a fresh session id. Any other failure (timeout, unreachable host) is returned as an error without touching the stored session.

## Safety notes

- Authenticate with SSH keys and `BatchMode=yes`. The bridge never handles passwords and stores no secrets.
- Everything you send becomes input to the remote session - do not pass credentials through it.
- The bridge adds no permission-bypass flags on its own. If you add one via `args`, that is an explicit choice on your side.

## Troubleshooting

- `ssh not found on PATH` - on Windows the server falls back to the Git for Windows and Windows OpenSSH locations; if both are missing, install OpenSSH or put `ssh.exe` on `PATH`.
- Remote replies look garbled on Windows peers - set `"shell": "cmd"`; quoting differs between `sh` and `cmd`.
- `No conversation found` - the stored session is gone; the bridge retries once with a fresh session automatically. If it keeps happening, the remote CLI is rejecting `--session-id`/`--resume`.

## Limitations

- Both machines need the same agent CLI on `PATH` (the server falls back to common Windows install locations for `ssh` only).
- No streaming: you receive the remote reply when the remote command finishes.
- One conversation per peer (a single stored session id), not per topic.
- The state file has no concurrency guard - run one bridge process per user account.
- Deliberately small: this is a bridge, not an agent framework.

## License

MIT
