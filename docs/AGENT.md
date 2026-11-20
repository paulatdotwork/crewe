# Agent mode

A coding agent that drives a **sandboxed Linux container** through a tiny
action protocol: the model emits one action per turn (`run`, `write`, `edit`,
`read`, `finish`), Crewe executes it, feeds the observation back, and repeats
until the model finishes or the step budget runs out. Admin-only, one run at a
time, live transcript in the browser at `/agent`.

Why a text protocol instead of native tool-calling: local and merged models
emit malformed `tool_calls` often, but they can follow "reply with one block"
reliably — and it is trivial to debug. File contents travel in raw fenced
blocks (`WRITE: path` / `EDIT: path` with `find` and `replace` fences), so
nothing is JSON-escaped.

## Setup

1. Build the sandbox images and egress network (requires Docker):

   ```bash
   bash crewe-sandbox/setup.sh
   ```

   Run it as root (or with passwordless sudo) so it can also install the
   **gateway firewall rule** — without that rule, sandboxed code can bypass
   the egress allowlist and reach services bound on your host (see Security).

2. Start the router. The agent uses your configured **coder role backend** by
   default; pick any backend per run from the coder dropdown, or set
   `CREWE_AGENT_URL` to force one.

## Choosing the model

The coder dropdown lists every backend the agent can drive, by id — the
browser only ever sends an id, the URL and key stay server-side:

- **OpenAI-compatible** backends (llama.cpp, vLLM, hosted APIs) — the
  `/v1/chat/completions` dialect.
- **Claude (Anthropic)** backends — driven natively over `/v1/messages`
  (system prompt hoisted, alternating turns, `max_tokens` required, no
  sampling parameters — current Claude models reject them). The backend must
  have a model name; the dropdown labels it `id · model`.

`default` is the router's *normal* coder tier, never the paid one — pick a
paid backend deliberately. Rate limits (429/529) and a busy llama.cpp (503)
are retried a bounded number of times; a backend whose output ceiling is
below the 32k cap is retried once at 8k.

## The workflow

- **Projects** — every run belongs to a project. With nothing selected, each
  run starts a brand-new project in a clean workspace. **Continue** loads a
  project's saved state (files, preview, summary) without any model call, and
  the next run seeds its workspace from that project's latest snapshot, with
  the previous summary as context.
- **Work in a real folder** — the Browse tab lists folders on the target and
  **Work here** pins one; the run then executes in that folder, editing files
  in place, instead of a fresh workdir. The folder is saved on the project, so
  continuing resumes there; switching target clears it. System folders
  (`/`, `/etc`, `/usr`, `/home` and the like) are refused, as is your whole
  home directory on this machine, and the sandbox target takes no folder (it
  is a throwaway container). Browsing this machine needs
  `CREWE_AGENT_ALLOW_LOCAL=1`.
- **Auto-plan** — small local models scope-creep; by default the task is first
  expanded into a bounded MVP spec (editable before running).
- **Workspace briefing** — before the first model turn the harness states the
  real working directory, whether it is sandboxed, which tools exist
  (`python3`, `node`, `npm`, `git`, `pip`), the files present on a continued
  project, and the **project instructions**: an `AGENTS.md` (or `CLAUDE.md`)
  in the workspace root — or in a freshly cloned repo — is loaded (4000-char
  clip) and shown in the transcript.
- **Notes mid-run** — type a note under the transcript while a run is live.
  It is delivered with the agent's next observation, after the action in
  flight completes (never mid-generation), and shown in the transcript when
  queued and when delivered. A note pending when the model tries to finish
  defers the finish by one step (only with steps to spare). Notes that could
  not be delivered are reported at the end, never dropped. Bounded: 2000
  chars, 20 waiting.
- **Preview** — HTML builds render in an iframe, served with real mimetypes
  and a CSP sandbox (see Security).
- **Test bench** — run one command in a fresh sandbox seeded from a run's
  snapshot; Python builds get a pre-filled entry command
  (`pip install -r requirements.txt && python3 main.py`).
- **Follow-ups** — "Improve it" re-runs the agent in the same project,
  auto-plan off, so follow-ups stay surgical.
- A task containing a GitHub URL is cloned by the harness (one model step
  saved) and always starts from a clean clone.

## Verification — deterministic, no critic model

Every check is mechanical and bounded; the harness never asks a second model
to judge the first.

- **Syntax checks** after every write/edit, on the host: Python (`ast`),
  JavaScript and inline `<script>` blocks (`node --check`), shell
  (`bash -n`), JSON. A check that cannot run (JSX, templated pages, JSONC,
  `node` missing) says nothing rather than "OK". The verdict rides on the
  write's own observation, so the model fixes it before moving on.
- **Finish gate** — `finish` is refused (at most twice, only with steps to
  spare) when source files changed and nothing ran since, or a syntax error
  is outstanding — flagged files are re-read first, so a `sed`/`rm` fix
  counts. It also refuses third-party imports that nothing records
  (`requirements.txt`, `pyproject.toml`, `package.json` and the like): the
  user receives the files, not the container the packages were installed in.
  Standard-library and project-local modules never count, and the refusal
  spends the same bounded budget, so it cannot block delivery.
- **Verdict** — every run ends with `✓ verified — <cmd> exited 0 after the
  last change` or `⚠ unverified — <why>`, in the transcript and the job
  status. "Done" is never claimed for code that was never run.
- **Offline browser check** — an HTML build is opened once in a headless
  Firefox that cannot reach the network (or your host); console errors and a
  blank screen are reported as remarks. `CREWE_AGENT_BROWSER_CHECK=0`
  disables it; pages that reference external URLs are skipped, not demoted.
- **Loop hygiene** — a failing command repeated with nothing changed gets a
  nag; three unparseable replies in a row end the run; a truncated write is
  flagged; `CREWE_AGENT_RUN_MAX` caps the whole run.

## Context window

The message list is the model's whole memory of a run, and long builds used
to outgrow small windows silently (a real run died with an HTTP 400 at step
11 on an 8k model). Now:

- The coder's window is read from llama.cpp's `/props` when available; the
  history budget follows it (75% of the window at ~3.2 chars/token), else
  `CREWE_AGENT_CTX_CHARS` applies. The transcript states both at run start.
- Older turns are **condensed** deterministically — the action header and the
  tail (exit code, last error line) survive, the bulk goes — while the newest
  turns stay verbatim. Under pressure the verbatim window shrinks to two
  turns, then old turns drop to their heads. The prefix (system prompt, task,
  briefing) is never touched. Observations are clipped proportionally to the
  window, and the transcript says once when even that cannot fit.

## Running without Docker (optional)

If you can't (or don't want to) run Docker, two targets work without it:

- **Local mode** — the agent works directly on the router's host in
  `~/crewe-agent/<run-id>`. **This is unsandboxed**: commands run for real,
  as the router's own user, with no egress allowlist and no resource limits
  — the model's code can do anything your user can. Because that is a real
  decision, the target does not exist until you opt in:

  ```bash
  CREWE_AGENT_ALLOW_LOCAL=1 python3 router.py
  ```

  A "⚠ local" target then appears in the dropdown, with the warning shown
  in the UI. The test bench also falls back to a throwaway local dir when
  Docker is absent and local mode is enabled. File writes/reads are contained
  to the per-run workdir as hygiene, but `run` commands are not contained at
  all — that's the trade you opted into.

- **An SSH target pointed at any machine you control** (including
  `127.0.0.1`) — see below. Also unsandboxed, but at least scoped to the
  account and machine you choose.

Prefer the sandbox when Docker is available: it is the only mode where
hostile code and cloned repos are actually confined.

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
| `CREWE_AGENT_RUN_MAX` | `14400` | hard wall-clock cap on a whole run |
| `CREWE_AGENT_CMD_TIMEOUT` | `600` | per-command budget inside the workspace |
| `CREWE_AGENT_OBS_LIMIT` | `12000` | max observation chars fed back to the model (scaled down for small windows) |
| `CREWE_AGENT_CTX_CHARS` | `360000` | history budget when the backend does not publish its window |
| `CREWE_AGENT_ANTHROPIC_MAX_TOKENS` | `32768` | output cap for Claude backends |
| `CREWE_AGENT_BROWSER_CHECK` | `1` | `0` disables the offline headless-Firefox check of HTML builds |
| `CREWE_AGENT_HOSTS` | `~/crewe_agent_hosts.json` | SSH target allowlist file |
| `CREWE_AGENT_ALLOW_LOCAL` | *(unset)* | `1` enables the unsandboxed local target |
| `CREWE_AGENT_LOCAL_DIR` | `~/crewe-agent` | where local-mode runs keep their workdirs |
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
- **Reads never follow symlinks**: the harness's own file reads (the model's
  `read`/`edit`, the automatic `AGENTS.md` briefing) refuse symlinks in every
  backend — a cloned repo shipping `AGENTS.md -> /etc/passwd` gets nothing.
  Inside the sandbox the model can still `cat` whatever the container holds;
  that is its own namespace.
- **Previews are model-written pages**: served with
  `Content-Security-Policy: sandbox allow-scripts`, which forces an opaque
  origin — the page cannot read the admin session or call `/agent/*` with
  credentials. The post-run browser check opens the page in a Firefox whose
  every request is proxied into a closed port, so model-written JS reaches
  neither the LAN nor the internet nor your host.
- **Downloads**: zip/file/preview routes are path-contained and skip
  symlinks, so a hostile repo cannot smuggle host files into a download.
- **Notes** are owner-gated like every job route: another user's session
  cannot see or steer your run.
- Runs are audited to `CREWE_AGENT_AUDIT_DIR` as append-only JSONL (URLs and
  model names only — never a key).
