# Agent mode

A coding agent that drives a **sandboxed Linux container** through a tiny
action protocol: the model emits one JSON action per turn (`run`, `write`,
`read`, `finish`), Crewe executes it, feeds the observation back, and repeats
until the model finishes or the step budget runs out. Admin-only, one run at a
time, live transcript in the browser at `/agent`.

Why a text protocol instead of native tool-calling: local and merged models
emit malformed `tool_calls` often, but they can follow "reply with one json
block" reliably — and it is trivial to debug.

## Setup

1. Build the sandbox images and egress network (requires Docker):

   ```bash
   bash crewe-sandbox/setup.sh
   ```

   Run it as root (or with passwordless sudo) so it can also install the
   **gateway firewall rule** — without that rule, sandboxed code can bypass
   the egress allowlist and reach services bound on your host (see Security).

2. Start the router. The agent uses your configured **coder role backend** by
   default; pick any openai-kind backend per run from the coder dropdown, or
   set `CREWE_AGENT_URL` to force one.

## The workflow

- **Projects** — every run belongs to a project. With nothing selected, each
  run starts a brand-new project in a clean workspace. **Continue** loads a
  project's saved state (files, preview, summary) without any model call, and
  the next run seeds its workspace from that project's latest snapshot, with
  the previous summary as context.
- **Auto-plan** — small local models scope-creep; by default the task is first
  expanded into a bounded MVP spec (editable before running).
- **Preview** — HTML builds render in an iframe, served with real mimetypes
  and a CSP sandbox (see Security).
- **Test bench** — run one command in a fresh sandbox seeded from a run's
  snapshot; Python builds get a pre-filled entry command
  (`pip install -r requirements.txt && python3 main.py`).
- **Follow-ups** — "Improve it" re-runs the agent in the same project,
  auto-plan off, so follow-ups stay surgical.
- A task containing a GitHub URL is cloned by the harness (one model step
  saved) and always starts from a clean clone.

## Remote SSH targets (optional)

The agent can work directly on a real machine over SSH instead of the Docker
sandbox. **These runs are not sandboxed** — commands run for real as that
host's user. Targets are declared in a file that lives OUTSIDE the repo
(default `~/crewe_agent_hosts.json`, override with `CREWE_AGENT_HOSTS`):

```json
{"hosts": [
  {"name": "buildbox", "host": "10.0.0.5", "user": "me",
   "port": 22, "key": "~/.ssh/id_ed25519", "workdir": "~/crewe-agent"}
]}
```

The allowlist is the security boundary: the browser only ever sends a target
*name*; host/user/key are resolved server-side, so even an admin session
cannot point the agent at an arbitrary machine.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `CREWE_AGENT_URL` | *(router's coder role)* | force a specific chat-completions endpoint |
| `CREWE_AGENT_MAX_STEPS` | `15` | default step budget (UI can pick 8–40 per run) |
| `CREWE_AGENT_READ_TIMEOUT` | `900` | seconds of stream *silence* before a call fails |
| `CREWE_AGENT_CALL_MAX` | `3600` | hard wall-clock cap on one model call |
| `CREWE_AGENT_CMD_TIMEOUT` | `600` | per-command budget inside the workspace |
| `CREWE_AGENT_OBS_LIMIT` | `12000` | max observation chars fed back to the model |
| `CREWE_AGENT_HOSTS` | `~/crewe_agent_hosts.json` | SSH target allowlist file |
| `CREWE_AGENT_AUDIT_DIR` | `~/llama_logs/agent` | append-only run audit logs |
| `CREWE_AGENT_AUDIT_KEEP` | `300` | audit files kept before oldest are dropped |
| `CREWE_SANDBOX_IMAGE` | `crewe-sandbox:latest` | sandbox container image |
| `CREWE_SANDBOX_USER` | `1000:1000` | container user; set `0:0` only if a task needs apt-get |

## Security model

- **Egress allowlist**: sandboxes live on an internal Docker network with no
  internet route; their only way out is a tinyproxy whose default-deny filter
  allows package mirrors (npm, PyPI, Debian) and GitHub. Edit
  `crewe-sandbox/filter` to change it — every entry is a hole.
- **Gateway rule (important)**: Docker's `--internal` blocks LAN/internet but
  *not* the host's own gateway IP. `setup.sh` installs
  `iptables -I INPUT -s <egress-subnet> -d <gateway> -j DROP` so sandboxed
  code cannot reach services bound on your host (the router itself, model
  servers). iptables rules do not survive reboots — persist with your
  distro's tooling.
- **Non-root sandbox**: containers run as uid 1000 with `--cap-drop ALL`,
  `no-new-privileges`, memory/pids limits, and no mounts.
- **Previews are model-written pages**: served with
  `Content-Security-Policy: sandbox allow-scripts`, which forces an opaque
  origin — the page cannot read the admin session or call `/agent/*` with
  credentials.
- **Downloads**: zip/file/preview routes are path-contained and skip
  symlinks, so a hostile repo cannot smuggle host files into a download.
- Runs are audited to `CREWE_AGENT_AUDIT_DIR` as append-only JSONL.
