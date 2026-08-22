"""agent.py — the Crewe code agent loop (Phase C, standalone).

A model drives a sandbox through a tiny action protocol: it emits ONE JSON
action per turn, we execute it in the Sandbox, feed the observation back, and
repeat until it emits `finish` or the step budget runs out. Every step is
written to an append-only audit log.

Deliberately a TEXT protocol (JSON-in-a-fenced-block), not native OpenAI
tool-calling: local/merged models emit malformed tool_calls often, but they can
follow "reply with one json block" reliably, and it's trivial to debug. Reads
message.content and strips any <think> the template leaks.

Standalone: imports sandbox.py only, never router.py. Phase D wires it in.
"""
import ast
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid

import requests

from sandbox import Sandbox

# Standalone default only: when embedded in the router, the router passes an
# explicit coder (chosen in the UI, or its own code backend). llama.cpp's
# conventional local port; deliberately NOT a machine-specific address.
DEFAULT_AGENT_URL = "http://127.0.0.1:8080/v1/chat/completions"
AUDIT_DIR = os.path.expanduser(
    os.environ.get("CREWE_AGENT_AUDIT_DIR", "~/llama_logs/agent"))

# Coder calls STREAM, so this bounds SILENCE (time between chunks / to the
# first token), not the whole generation — a slow-but-alive model can generate
# for an hour, while a wedged server still fails in minutes. First-token
# silence includes prompt processing, which on a big context is minutes.
READ_TIMEOUT = int(os.environ.get("CREWE_AGENT_READ_TIMEOUT", "900"))
# Hard wall-clock cap on ONE model call — the backstop against a generation
# spiral that keeps emitting tokens forever (repetition loops stream happily).
CALL_MAX_SECS = int(os.environ.get("CREWE_AGENT_CALL_MAX", "3600"))
# Per-command budget for `run` actions (installs, builds, test suites).
CMD_TIMEOUT = int(os.environ.get("CREWE_AGENT_CMD_TIMEOUT", "600"))
# Cap on how much of an observation (file read / command output) enters the
# MODEL's context. One `cat` of a big log used to flood the whole window and
# starve every later step. The full text still reaches the audit log's obs
# preview and the sandbox — only the model sees the trimmed middle.
OBS_LIMIT = int(os.environ.get("CREWE_AGENT_OBS_LIMIT", "12000"))
# Whole-run wall clock. Per-call and per-command caps bound one step, not the
# run: 40 steps of a slow coder could otherwise run for days. 0 disables.
RUN_MAX_SECS = int(os.environ.get("CREWE_AGENT_RUN_MAX", "14400"))
# Consecutive unparseable replies before the run stops — a model that cannot
# follow the protocol must not burn the whole budget on retries.
MAX_PARSE_FAILS = 3
# How many times `finish` is refused for lack of verification. Bounded, so a
# model that genuinely cannot run anything still ends (flagged unverified).
MAX_FINISH_BOUNCES = 2
# Only SOURCE files oblige the model to run something before finishing;
# everything else (html/css/docs/config, Makefile, .gitignore…) is judged by
# the syntax checks alone. Listing the runnable kinds, not the static ones,
# means an unknown extension never costs a refusal.
RUNNABLE_EXTS = (".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".sh",
                 ".bash", ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".java",
                 ".rb", ".php", ".swift", ".kt", ".gd", ".lua", ".cs", ".pl")
# `edit` round-trips the whole file through read_file; a file past this size
# is refused rather than silently truncated on the way back.
EDIT_MAX_BYTES = 2_000_000

# ---- context window management (deterministic, no model calls) --------------
# The message list is the model's whole memory of the run, and it only grew:
# every file it wrote and every observation it got stayed verbatim. A real run
# died with an HTTP 400 at step 11 on an 8k-context model (three whole files in
# the history by then). Older turns are now CONDENSED to their heads and tails
# (the action header, the exit code, the last error line survive; the bulk
# goes) while the newest turns stay verbatim. The budget adapts to the coder's
# real window when the server publishes it (llama.cpp /props); otherwise the
# CREWE_AGENT_CTX_CHARS default (~100k tokens) applies.
CTX_CHARS = int(os.environ.get("CREWE_AGENT_CTX_CHARS", "360000"))
CTX_CHARS_PER_TOKEN = 3.2         # conservative for code and JSON
CTX_RESERVE = 0.25                # share of the window kept free for the reply
KEEP_RECENT_STEPS = 6             # newest turns always verbatim
CONDENSE_OLD_AFTER = 600          # an old turn longer than this gets condensed
CONDENSE_HEAD, CONDENSE_TAIL = 400, 200

# ---- anthropic dialect --------------------------------------------------------
# The loop speaks OpenAI chat-completions by default; an Anthropic Messages
# backend is driven natively (top-level system, alternating turns, a required
# max_tokens). 32768 and not 8192 for the cap: a cap that binds MID-FILE
# truncates a write, and Anthropic bills tokens used, not the cap.
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MAX_TOKENS = int(os.environ.get("CREWE_AGENT_ANTHROPIC_MAX_TOKENS", "32768"))

# ---- notes from the user during a run ------------------------------------------
# Steering by the human, delivered with the next observation (never mid-
# generation). Bounded by the caller; clipped here as a backstop.
NOTE_MAX_CHARS = 2000

# ---- project instructions --------------------------------------------------------
# A workspace that carries an AGENTS.md (or CLAUDE.md) is briefed with it,
# the way every current coding agent does; bounded so a novel of a file
# cannot eat the window.
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
INSTRUCTIONS_MAX = 4000


class AgentCancelled(Exception):
    """Raised inside _chat when the caller's should_cancel fires mid-stream —
    lets Stop land within a chunk instead of after a whole generation."""


def _agent_url():
    # read per call, not at import time, so setting CREWE_AGENT_URL after this
    # module is first imported (e.g. by a long-lived router process) still works
    return os.environ.get("CREWE_AGENT_URL", DEFAULT_AGENT_URL)

# ---- native tool calling (OpenAI function calling) --------------------------
# A capable coder emits well-formed tool_calls, which
# is a more robust channel than regex-parsing a ```json block out of prose. The
# text protocol stays as the universal fallback: a model that ignores the tools
# and replies with a WRITE:/```json block still works, and a backend that
# rejects the `tools` param drops the whole run to text mode. One observation
# per action either way — the verification loop is unchanged. Kill switch:
# CREWE_AGENT_TOOLS=0.
TOOLS_ENABLED = os.environ.get("CREWE_AGENT_TOOLS", "1") != "0"

def _tool_schemas():
    obj = lambda props, req: {"type": "object", "properties": props,
                              "required": req, "additionalProperties": False}
    fn = lambda name, desc, props, req: {
        "type": "function",
        "function": {"name": name, "description": desc,
                     "parameters": obj(props, req)}}
    S = {"type": "string"}
    return [
        fn("run", "Run a shell command in the workspace and observe its "
           "output. Use this to install packages, run the program, and run "
           "tests.", {"cmd": S}, ["cmd"]),
        fn("write", "Create or overwrite a file with the given contents.",
           {"path": S, "content": S}, ["path", "content"]),
        fn("edit", "Replace ONE exact occurrence of `find` with `replace` in "
           "an existing file. Prefer this over write for small changes.",
           {"path": S, "find": S, "replace": S}, ["path", "find", "replace"]),
        fn("read", "Read a file back from the workspace.", {"path": S}, ["path"]),
        fn("finish", "Signal the task is complete. Only after you have run the "
           "program or its tests and read the output.", {"summary": S},
           ["summary"]),
    ]

AGENT_TOOLS = _tool_schemas()

TOOL_SYSTEM = """You are a coding agent. You complete the task by calling the
provided tools, ONE step at a time, in a workspace directory. The WORKSPACE
STATE message tells you where you are, which tools exist, and what is there.

The tools are: run (a shell command), write (create/overwrite a file), edit
(replace one exact occurrence in a file), read (read a file back), and finish
(you are done). Call a tool — do not answer in prose.

Rules:
- After each tool call you get its result. Use it. Do not imagine results.
- Every write/edit is syntax-checked for you; fix a reported error before
  moving on.
- Prefer edit over write for changes to an existing file.
- Install packages you need with pip/npm (via run).
- VERIFY before you finish: run the program or its tests and read the output.
  finish is REFUSED if you changed files and have not run anything since.
- Never repeat a command that already failed the same way; change something first.
- Relative paths resolve in the workspace. Do not cd elsewhere."""


def _toolcall_to_action(tc):
    """Map a tool_call {name, arguments} to the action dict _do_action wants.
    Returns (action, error) — error is a string when the arguments were not
    valid JSON (fed back verbatim so the model can retry), else None."""
    name = tc.get("name") or ""
    raw = tc.get("arguments")
    err = None
    if isinstance(raw, dict):
        args = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            args = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            args, err = {}, ("the arguments for tool '" + name + "' were not "
                             "valid JSON — send a single well-formed JSON object")
    else:
        args = {}
    if not isinstance(args, dict):
        args = {}
    # the tool NAME is authoritative — a payload key must never override it
    # (a `write` call whose arguments carry {"action":"run"} would otherwise
    # run a shell command), and _-prefixed keys are harness-internal
    action = {k: v for k, v in args.items()
              if not (isinstance(k, str) and k.startswith("_"))}
    action["action"] = name
    return action, err


SYSTEM = """You are a coding agent. You complete the task by taking ONE action at
a time in a workspace directory and observing the result. The WORKSPACE STATE
message tells you where you are, which tools exist, and what is already there.

Reply with EXACTLY ONE action per reply, and nothing else. The actions are:

  {"action":"run",   "cmd":"<shell command>"}      run a shell command in the workspace
  {"action":"write", "path":"<file>", "content":"<file contents>"}   create or overwrite a file
  {"action":"edit",  "path":"<file>", "find":"<exact text>", "replace":"<new text>"}   replace ONE exact occurrence
  {"action":"read",  "path":"<file>"}              read a file back
  {"action":"finish","summary":"<what you built, and what you ran to verify it>"}   you are done

A JSON action goes in a ```json fenced block. For files, PREFER these RAW
forms (no JSON escaping — the contents go between the fences exactly as they
should appear on disk):

  WRITE: path/to/file.py
  ```
  <complete file contents>
  ```

  EDIT: path/to/file.py
  ```find
  <a few exact lines that occur ONCE in the file, copied verbatim>
  ```
  ```replace
  <the lines that replace them>
  ```

Rules:
- After each action you get an OBSERVATION. Use it. Do not imagine results.
- Every write/edit is syntax-checked for you; fix a reported error before moving on.
- Prefer edit over write for changes to an existing file: an edit is small and
  exact, while rewriting a big file is slow and risks a truncated reply.
- Install packages you need with pip/npm.
- VERIFY before you finish: run the program or its tests and read the output.
  finish is REFUSED if you changed files and have not run anything since.
- Never repeat a command that already failed the same way; change something first.
- Relative paths resolve in the workspace. Do not cd elsewhere.
- One action per reply. No prose outside the block.

Example reply:
```json
{"action":"run","cmd":"python3 main.py"}
```"""


PLANNER_SYSTEM = (
    "You are a planning assistant for a coding agent with a SMALL local model "
    "and a limited step budget (~15 actions). Turn the request into a BOUNDED, "
    "ACHIEVABLE spec — a minimal working slice, NOT an ambitious app. A small "
    "finished thing beats a large skeleton.\n\n"
    "FIRST, honor the KIND of app the user asked for — never change its nature:\n"
    "- Map, chart, UI, game, animation, or 'a nice app' with ANY visual element "
    "→ plan a WEB app: prefer a SINGLE self-contained index.html (inline CSS/JS). "
    "Acceptance = 'open index.html in a browser and see <the visual>'.\n"
    "- Script, tool, CLI, parser, or data task → plan a CLI, testable from the "
    "command line.\n"
    "- Kind unspecified → pick the simplest runnable form (usually a CLI).\n"
    "NEVER silently downgrade a requested map/UI/visual to a command-line tool.\n\n"
    "Rules:\n"
    "- Smallest version that still demonstrates the idea; one language, min deps.\n"
    "- Include ONE concrete acceptance check (a command to run, OR 'open <file>, "
    "see <X>').\n"
    "- Be concise. Output ONLY the spec, in exactly this shape:\n\n"
    "Scope: <one sentence>\n"
    "Features:\n- <3 to 6 concrete, minimal features>\n"
    "Acceptance:\n- <a specific check that proves it works>\n"
    "Out of scope: <what you deliberately cut to fit the budget>"
)


def _plan(task, coder=None, should_cancel=None):
    """One model call: expand a request into a bounded, buildable spec. Short
    silence timeout — a plan is a small generation, so a long stall = a stuck
    coder, not real work; fail fast and let the run fall back to the raw task."""
    return _chat([{"role": "system", "content": PLANNER_SYSTEM},
                  {"role": "user", "content": f"Request: {task}"}],
                 temperature=0.3, max_tokens=700, timeout=(10, 150), coder=coder,
                 should_cancel=should_cancel)


def _anthropic_body(messages, temperature, max_tokens, model):
    """OpenAI-shaped messages -> Anthropic Messages body. Four things differ
    and each one 400s if missed: system prompts are a TOP-LEVEL string, turns
    must alternate user/assistant starting with user (the loop opens with two
    user messages — TASK and WORKSPACE STATE — so same-role neighbours are
    merged), max_tokens is required, and sampling parameters are not sent
    (`temperature` is ignored here)."""
    systems, msgs = [], []
    for m in messages:
        role, content = m.get("role"), m.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        if role == "system":
            if content:
                systems.append(content)
            continue
        role = "assistant" if role == "assistant" else "user"
        if not content.strip():
            # the API rejects an empty text turn; an empty model reply (a
            # parse failure) must not poison every call after it
            content = "(empty reply)"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n\n" + content
        else:
            msgs.append({"role": role, "content": content})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if not msgs:
        msgs = [{"role": "user", "content": " "}]
    # no sampling parameters: current Claude models REJECT temperature
    # ("deprecated for this model" — a real 400 from the first live run), and
    # the loop's 0.2 was only ever a llama.cpp determinism nudge
    body = {"model": model, "messages": msgs, "max_tokens": int(max_tokens),
            "stream": True}
    if systems:
        body["system"] = "\n\n".join(systems)
    return body


def _chat(messages, temperature=0.2, max_tokens=None, timeout=None, coder=None,
          should_cancel=None, on_progress=None, tools=None):
    """One coder call, STREAMED.

    coder: optional {"url","key"?,"model"?,"kind"?} choosing which model drives
    the loop (the router resolves it server-side from its backend list);
    default is the CREWE_AGENT_URL / conventional-local coder. kind "openai"
    (default) speaks /v1/chat/completions; kind "anthropic" speaks
    /v1/messages natively — that is what lets a Claude backend drive the loop.
    max_tokens: None = the dialect's default (8192 openai, ANTHROPIC_MAX_TOKENS).
    should_cancel: checked between chunks — Stop lands in ~a chunk, not after
    a whole multi-minute generation. Raises AgentCancelled.
    on_progress: called with the running character count, throttled by the
    caller's needs — fine to call every chunk.

    Streaming is why the timeouts are sane: the (connect, read) tuple bounds
    connect and SILENCE — a live generation resets the read clock with every
    chunk, so slow long writes never false-timeout, while a wedged server
    still dies in READ_TIMEOUT. CALL_MAX_SECS backstops repetition spirals
    that stream forever. One retry on a transport drop (llama.cpp restarts,
    flaky LAN) — never on timeout or cancel."""
    coder = coder or {}
    kind = (coder.get("kind") or "openai").lower()
    url = coder.get("url") or _agent_url()
    if kind == "anthropic":
        if not coder.get("model"):
            raise RuntimeError("anthropic coder needs a model name — set it on the backend")
        headers = {"anthropic-version": ANTHROPIC_VERSION,
                   "content-type": "application/json"}
        if coder.get("key"):
            headers["x-api-key"] = coder["key"]
        body = _anthropic_body(messages, temperature,
                               max_tokens or ANTHROPIC_MAX_TOKENS, coder["model"])
    else:
        headers = {"Authorization": f"Bearer {coder['key']}"} if coder.get("key") else {}
        body = {
            "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens or 8192, "stream": True,
            # Thinking OFF: the ReAct loop IS the reasoning (act→observe→repeat), and
            # a thinking model's reasoning tokens were eating the output budget and
            # truncating big writes (a whole-file write would arrive cut off). Same
            # lesson the code pipeline learned. Harmless kwarg on models that ignore it.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if coder.get("model"):
            body["model"] = coder["model"]
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
            # a hint, not a guarantee (llama.cpp may still batch) — the loop
            # handles 1..N calls per reply either way
            body["parallel_tool_calls"] = False
    drops = rate = 0
    lowered = False
    while True:
        try:
            return _stream_once(url, headers, body, timeout or (10, READ_TIMEOUT),
                                should_cancel, on_progress, kind,
                                want_tools=bool(tools) and kind != "anthropic")
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError):
            if drops:
                raise
            drops += 1
            time.sleep(3)
        except requests.exceptions.HTTPError as e:
            st = getattr(e.response, "status_code", None)
            msg = str(e)
            # a backend whose output ceiling is below our cap 400s on every
            # step — once, fall back to the classic cap rather than dying
            if (kind == "anthropic" and st == 400 and "max_tokens" in msg
                    and not lowered and int(body.get("max_tokens") or 0) > 8192):
                body["max_tokens"] = 8192
                lowered = True
                continue
            # rate limit / overloaded / no free slot: routine on a hosted API
            # and on a busy llama.cpp — bounded retries, cancel-aware waits
            if st in (429, 503, 529) and rate < 2:
                rate += 1
                try:
                    wait = int(e.response.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0
                wait = min(max(wait, (5, 15)[rate - 1]), 30)
                for _ in range(wait):
                    if should_cancel and should_cancel():
                        raise AgentCancelled()
                    time.sleep(1)
                continue
            raise


def _norm_tool_calls(raw):
    """Normalise an OpenAI tool_calls array to [{id,name,arguments(str)}]."""
    out = []
    for c in raw or []:
        fn = c.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, (dict, list)):
            args = json.dumps(args)
        out.append({"id": c.get("id"), "name": fn.get("name"),
                    "arguments": args if isinstance(args, str) else ""})
    return out


def _stream_once(url, headers, body, timeout, should_cancel, on_progress,
                 kind="openai", want_tools=False):
    started = time.time()
    with requests.post(url, timeout=timeout, headers=headers, json=body,
                       stream=True) as r:
        if r.status_code >= 400:
            # the body says WHY ("exceeds the available context size", a bad
            # model name); "400 Client Error" alone told a postmortem nothing.
            # Only a SMALL, length-declared body is read: reading an unbounded
            # (chunked, dripping) error body would hold the call open past
            # every timeout, and the status alone is still an honest error.
            detail = ""
            try:
                clen = int(r.headers.get("Content-Length") or 0)
                if 0 < clen <= 2048:
                    detail = r.content[:300].decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
            raise requests.exceptions.HTTPError(
                f"{r.status_code} {r.reason} for url: {url}"
                + (f" — {detail}" if detail else ""), response=r)
        ctype = r.headers.get("Content-Type", "")
        if "text/event-stream" not in ctype:
            # backend ignored stream:true and sent one JSON body — accept it,
            # but report error-shaped 200s legibly ("model error: 'choices'"
            # told the admin nothing about what the backend actually said)
            try:
                data = r.json()
            except ValueError:
                raise RuntimeError(f"coder returned non-JSON: {r.text[:200]!r}")
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"coder error: {str(data['error'])[:300]}")
            if kind == "anthropic":
                parts = [blk.get("text", "") for blk in (data.get("content") or [])
                         if isinstance(blk, dict) and blk.get("type") == "text"]
                text = "".join(parts)
                if not text and data.get("stop_reason") == "refusal":
                    text = "(the model declined this request)"
                return _strip_think(text)
            try:
                msg = data["choices"][0]["message"]
            except (KeyError, IndexError, TypeError):
                raise RuntimeError(f"coder reply missing choices: {str(data)[:200]}")
            if want_tools:
                return {"content": _strip_think(msg.get("content") or ""),
                        "tool_calls": _norm_tool_calls(msg.get("tool_calls"))}
            return _strip_think(msg.get("content") or "")
        # byte-level reads, lines assembled by hand: iter_lines buffers up to
        # its chunk_size before yielding, which held cancel checks hostage for
        # seconds on a slow stream. A generation is ≤ ~100KB, so per-byte
        # iteration costs nothing and cancel/timeout react at wire speed.
        parts, buf = [], b""
        tool_acc = {}                 # index -> {id,name,args}
        n = 0
        for byte in r.iter_content(chunk_size=1):
            if should_cancel and should_cancel():
                raise AgentCancelled()
            if time.time() - started > CALL_MAX_SECS:
                raise TimeoutError(
                    f"model call exceeded CREWE_AGENT_CALL_MAX={CALL_MAX_SECS}s "
                    f"({n} chars streamed) — likely a generation spiral")
            buf += byte
            if not buf.endswith(b"\n"):
                continue
            line = buf.decode("utf-8", "replace").strip()
            buf = b""
            if not line.startswith("data:"):
                continue                  # `event:` lines carry no payload
            data = line[5:].strip()
            if data == "[DONE]":
                break
            if kind == "anthropic":
                try:
                    ev = json.loads(data)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if t == "content_block_delta":
                    d = ev.get("delta") or {}
                    piece = d.get("text", "") if d.get("type") == "text_delta" else ""
                elif t == "error":
                    raise RuntimeError("coder error: " + str(
                        (ev.get("error") or {}).get("message") or ev)[:300])
                elif t == "message_delta":
                    # a safety decline is a normal 200 with stop_reason
                    # "refusal" and no text — name it, or it reads as an
                    # empty reply and burns a parse-fail for no reason
                    stop = (ev.get("delta") or {}).get("stop_reason")
                    piece = ("(the model declined this request)"
                             if stop == "refusal" and not parts else "")
                elif t == "message_stop":
                    break
                else:
                    piece = ""
            else:
                try:
                    delta = json.loads(data)["choices"][0].get("delta", {})
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                piece = delta.get("content") or ""
                if want_tools:
                    for tc in (delta.get("tool_calls") or []):
                        idx = tc.get("index", 0)
                        acc = tool_acc.setdefault(idx, {"id": None, "name": None, "args": ""})
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            acc["name"] = fn["name"]
                        frag = fn.get("arguments")
                        if frag:
                            acc["args"] += frag
                            n += len(frag)
                            if on_progress:
                                on_progress(n)
            if piece:
                parts.append(piece)
                n += len(piece)
                if on_progress:
                    on_progress(n)
        if want_tools:
            calls = [{"id": tool_acc[i]["id"], "name": tool_acc[i]["name"],
                      "arguments": tool_acc[i]["args"]}
                     for i in sorted(tool_acc)]
            return {"content": _strip_think("".join(parts)), "tool_calls": calls}
        return _strip_think("".join(parts))


def _strip_think(s):
    return re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL).strip()


def _clip_obs(obs, limit=None):
    """Trim the MIDDLE of an oversized observation. Head and tail both matter
    (a build log's error is at the end, a file's imports at the top); the
    marker tells the model the cut happened so it can narrow its next command."""
    limit = limit or OBS_LIMIT
    if len(obs) <= limit:
        return obs
    half = limit // 2
    return (obs[:half] + f"\n…[{len(obs) - limit} chars omitted from the middle "
            "— output too large; use grep/head/tail or read a specific part]…\n"
            + obs[-half:])


def _condense_text(text, head=CONDENSE_HEAD, tail=CONDENSE_TAIL):
    """Keep the head and tail of an old turn (the action header and the start
    of a file; the exit code and the last error line); drop the bulk with a
    marker that says so. Short turns pass through untouched."""
    if len(text) <= head + tail + 120:
        return text
    cut = len(text) - head - tail
    h, t = text[:head], text[-tail:]
    if h.count("```") % 2:
        # cut inside a fenced block (a WRITE): close it in the head and reopen
        # it before the tail, so the marker never reads as file content and
        # the turn never looks like a truncated write
        h += "\n```"
        t = "```\n" + t
    return (h + f"\n…[{cut} chars of this older turn condensed to save context "
            "— its effect is in the workspace (a write landed in full); re-read "
            "a file or re-run a command if you need the details]…\n" + t)


def _msg_len(m):
    """Byte-ish size of a message including tool-call arguments — a write's
    file body rides in tool_calls[].function.arguments in tool mode, not in
    content, so budgeting on content alone would miss the bulk."""
    n = len(m.get("content") or "")
    for tc in (m.get("tool_calls") or []):
        n += len(((tc.get("function") or {}).get("arguments")) or "")
    return n


def _condense_tool_calls(tcs):
    """Shrink big file bodies inside old tool-call arguments, keeping the JSON
    valid and the path/cmd intact — the write already landed in the workspace."""
    out = []
    for tc in tcs:
        fn = dict(tc.get("function") or {})
        args = fn.get("arguments") or ""
        if len(args) > CONDENSE_HEAD + CONDENSE_TAIL + 120:
            try:
                d = json.loads(args)
                for k in ("content", "replace", "find", "cmd"):
                    v = d.get(k)
                    if isinstance(v, str) and len(v) > 200:
                        d[k] = (v[:120] + f"…[{len(v) - 120} chars elided — the "
                                "change landed in the workspace]…")
                fn["arguments"] = json.dumps(d)
            except (json.JSONDecodeError, ValueError):
                fn["arguments"] = args[:CONDENSE_HEAD] + "…[elided]…"
        out.append({**tc, "function": fn})
    return out


def _condense_history(messages, start, condensed, budget=None,
                      keep_recent=KEEP_RECENT_STEPS):
    """Condense old turns IN PLACE so the run keeps fitting the model's window.

    messages[start:] is the step history (assistant reply, observation, ...).
    The newest `keep_recent` turns (two messages each) stay verbatim. Level 1:
    older turns longer than CONDENSE_OLD_AFTER are cut to head+tail. If the
    total is still above `budget`, level 2 shrinks the verbatim window to two
    turns; if still above, level 3 cuts condensed turns down to their heads.
    Deterministic and idempotent — `condensed` maps index -> level, so a turn
    is never cut twice at the same level and the prefix (system prompt, task,
    workspace briefing) is never touched. Returns (newly_condensed,
    level_reached, total_chars)."""
    def total():
        return sum(_msg_len(m) for m in messages)

    new, level = 0, 0

    def cut(keep, lvl):
        nonlocal new, level
        end = max(start, len(messages) - 2 * keep)
        for i in range(start, end):
            have = condensed.get(i, 0)
            c = messages[i].get("content") or ""
            tcs = messages[i].get("tool_calls")
            if lvl < 3:
                if have or _msg_len(messages[i]) <= CONDENSE_OLD_AFTER:
                    continue
                if c:
                    messages[i]["content"] = _condense_text(c)
                if tcs:
                    messages[i]["tool_calls"] = _condense_tool_calls(tcs)
            else:
                if have >= 3 or (len(c) <= 340 and not tcs):
                    continue
                if tcs:
                    messages[i]["tool_calls"] = _condense_tool_calls(tcs)
                if len(c) > 340:
                    h = c[:200]
                    if h.count("```") % 2:
                        h += "\n```"
                    messages[i]["content"] = (
                        h + "\n…[older turn condensed to its head — its effect "
                        "(e.g. a full file write) is in the workspace]…")
            condensed[i] = lvl
            new += 1
            level = max(level, lvl)

    cut(keep_recent, 1)
    if budget and total() > budget:
        cut(2, 2)
    if budget and total() > budget:
        cut(2, 3)
    return new, level, total()


def _ctx_budget(ctx_tokens):
    """History budget in chars for a window of ctx_tokens (None = unknown)."""
    if ctx_tokens and int(ctx_tokens) > 0:
        return int(int(ctx_tokens) * (1 - CTX_RESERVE) * CTX_CHARS_PER_TOKEN)
    return CTX_CHARS


def _probe_ctx(coder):
    """The coder's per-slot context window, if its server publishes one:
    llama.cpp answers /props with default_generation_settings.n_ctx; other
    servers 404. Only a CONFIGURED coder is probed (an explicit url or
    CREWE_AGENT_URL) — the bare built-in default is not. None = unknown."""
    coder = coder or {}
    if (coder.get("kind") or "openai") != "openai":
        return None
    url = coder.get("url") or os.environ.get("CREWE_AGENT_URL")
    if not url:
        return None
    base = url.split("/v1/", 1)[0]
    headers = {"Authorization": f"Bearer {coder['key']}"} if coder.get("key") else {}
    try:
        r = requests.get(base + "/props", timeout=(2, 2), headers=headers,
                         allow_redirects=False)
        if r.status_code != 200:
            return None
        n = (r.json().get("default_generation_settings") or {}).get("n_ctx")
        n = int(n)
        return n if n > 0 else None
    except Exception:
        return None


def _project_instructions(sb, roots=("",)):
    """The workspace's AGENTS.md (or CLAUDE.md), clipped, for the briefing.
    Returns (text, path) or (None, None). Each root is tried in order; a
    missing file is the common case and costs one read."""
    for root in roots:
        for name in INSTRUCTION_FILES:
            path = root + name
            try:
                # backends clip by BYTES; ask for the worst-case byte count of
                # INSTRUCTIONS_MAX chars so a multibyte file is not cut
                # silently below the char limit
                c = sb.read_file(path, max_bytes=INSTRUCTIONS_MAX * 4 + 1)
            except Exception:
                c = None
            if not c or not c.strip():
                continue
            clipped = (len(c) > INSTRUCTIONS_MAX
                       or len(c.encode("utf-8", "replace")) > INSTRUCTIONS_MAX * 4)
            c = c[:INSTRUCTIONS_MAX].rstrip("\ufffd")
            if clipped:
                c += "\n…[instructions clipped]…"
            return c, path
    return None, None


def _json_action(span):
    """A dict with an 'action' key, or None. strict=False: literal newlines
    and tabs inside strings are accepted — a multi-line shell command or a
    heredoc in "cmd" used to be a parse_fail (observed: a run lost a step on
    `python3 -c "<several lines>"`)."""
    try:
        obj = json.loads(span, strict=False)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) and "action" in obj else None


# raw EDIT:  EDIT: <path>\n```find\n<old>\n```\n```replace\n<new>\n```
# The replace block closes at the LAST fence when the reply ends there (the
# new text may itself contain ``` ), else at the first one.
_EDIT_HEAD = r"(?im)^[ \t]*EDIT:[ \t]*(\S+)[ \t]*\r?\n```find[ \t]*\r?\n(.*?)```[ \t]*\r?\n\s*```replace[ \t]*\r?\n"
_EDIT_LAST_RE = re.compile(_EDIT_HEAD + r"(.*)```[ \t]*\r?\n?\s*$", re.S)
_EDIT_RE = re.compile(_EDIT_HEAD + r"(.*?)```", re.S)
_WRITE_HDR = re.compile(r"(?im)^[ \t]*WRITE:")
_EDIT_HDR = re.compile(r"(?im)^[ \t]*EDIT:")


def _one_nl(s):
    """Drop the single newline that precedes a closing fence, so a block's
    lines are exactly the lines between the fences."""
    return re.sub(r"\r?\n\Z", "", s)


def _parse_edit(text):
    m = _EDIT_LAST_RE.search(text) or _EDIT_RE.search(text)
    if not m:
        return None
    return {"action": "edit", "path": m.group(1).strip(),
            "find": _one_nl(m.group(2)), "replace": _one_nl(m.group(3))}


def _parse_write(text):
    # GREEDY to the LAST fence: the file itself may contain ``` blocks (a
    # README with usage examples) — stopping at the first inner fence silently
    # truncated such files. The protocol allows nothing after the block, so
    # the last fence in the reply is the closer.
    m = re.search(r"(?im)^[ \t]*WRITE:[ \t]*(\S+)[ \t]*\r?\n```[\w.\-]*\r?\n(.*)\r?\n```[ \t]*\r?\n?\s*$",
                  text, flags=re.DOTALL)
    if m:
        return {"action": "write", "path": m.group(1).strip(), "content": m.group(2)}
    m = re.search(r"(?im)^[ \t]*WRITE:[ \t]*(\S+)[ \t]*\r?\n```[\w.\-]*\r?\n(.*?)\r?\n?```",
                  text, flags=re.DOTALL)
    if m:
        return {"action": "write", "path": m.group(1).strip(), "content": m.group(2)}
    # lenient: WRITE with an UNCLOSED fence (reply truncated before the closing
    # ``` ) — take everything after the opening fence so a big write still lands
    # and the agent can run/fix it, instead of dead-looping on parse_fail. The
    # _truncated flag becomes a warning in the observation: the model must be
    # told its file is probably incomplete, or it will run it and trust it.
    m = re.search(r"(?im)^[ \t]*WRITE:[ \t]*(\S+)[ \t]*\r?\n```[\w.\-]*\r?\n(.+)",
                  text, flags=re.DOTALL)
    if m:
        content = re.sub(r"\r?\n?```\s*$", "", m.group(2))
        return {"action": "write", "path": m.group(1).strip(), "content": content,
                "_truncated": True}
    return None


_ACTION_START = re.compile(r"(?im)^[ \t]*(?:WRITE:|EDIT:|```json\b)")
_FENCE_LINE = re.compile(r"(?m)^[ \t]*```")


def _split_first(text):
    """(first, rest): cut the reply before a SECOND action that follows a
    closed first block. A raw block closes at the last fence by design (file
    contents may hold fences), which used to swallow a trailing ```json run
    into the file. The cut is taken only where the first block is provably
    closed — an even number of fence lines lies between the two starts — and
    the remainder parses as an action on its own; a README that merely
    contains a ```json example inside its open fence is left whole."""
    starts = [m.start() for m in _ACTION_START.finditer(text)]
    if len(starts) < 2:
        return text, None
    first = starts[0]
    for cand in starts[1:]:
        fences = len(_FENCE_LINE.findall(text[first:cand]))
        if fences >= 2 and fences % 2 == 0 and _parse_one(text[cand:]):
            return text[:cand], text[cand:]
    return text, None


def _parse_action(text):
    """Pull the FIRST action out of the reply (anything after it is reported
    by _count_actions, never executed)."""
    first, _rest = _split_first(text)
    return _parse_one(first)


def _parse_one(text):
    """One action from a reply fragment. Raw WRITE/EDIT blocks are checked
    before JSON because their contents are arbitrary code that must NOT be
    JSON-parsed; whichever raw header comes FIRST wins, so a file that merely
    documents the other form is still a plain write."""
    w, e = _WRITE_HDR.search(text), _EDIT_HDR.search(text)
    order = []
    if w and e:
        order = [_parse_write, _parse_edit] if w.start() < e.start() else [_parse_edit, _parse_write]
    elif w:
        order = [_parse_write]
    elif e:
        order = [_parse_edit]
    for fn in order:
        a = fn(text)
        if a:
            return a
    # prefer fenced blocks
    blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    candidates = blocks + [text]
    for c in candidates:
        c = c.strip()
        # try the whole thing, then the first {...} span
        for span in ([c] + re.findall(r"\{.*\}", c, flags=re.DOTALL)):
            obj = _json_action(span)
            if obj:
                return obj
    return None


def _count_actions(text):
    """How many actions a reply contains, for the 'only the first was
    executed' note — the same split the executor uses, so headers INSIDE a
    written file never count. Never below 1."""
    n, t = 0, text
    for _ in range(20):
        first, rest = _split_first(t)
        if _parse_one(first):
            n += 1
        if not rest:
            break
        t = rest
    return max(n, 1)


def _apply_edit(content, find, repl):
    """Replace ONE occurrence of `find` in `content`. Exact match first; then
    a whitespace-tolerant pass (trailing whitespace ignored per line), because
    models copy indentation imperfectly. Returns (new_content, None) or
    (None, why) — ambiguity is an error, never a guess."""
    if not isinstance(find, str) or not find.strip():
        return None, "edit failed: 'find' must be a non-empty string"
    n = content.count(find)
    if n == 1:
        i = content.find(find)
        if (repl == "" and (i == 0 or content[i - 1] == "\n")
                and content.startswith(find + "\n", i)):
            # the find text is whole line(s): deleting it deletes the line(s),
            # not just the text (which would leave a blank line behind). A
            # partial-line find keeps the rest of its line.
            return content[:i] + content[i + len(find) + 1:], None
        return content.replace(find, repl, 1), None
    if n > 1:
        return None, (f"edit failed: the find text occurs {n} times — include "
                      "more surrounding lines so it matches exactly once")
    src = content.split("\n")
    want = [l.rstrip() for l in find.split("\n")]
    norm = [l.rstrip() for l in src]
    hits = [i for i in range(len(norm) - len(want) + 1)
            if norm[i:i + len(want)] == want]
    if len(hits) == 1:
        i = hits[0]
        new = src[:i] + (repl.split("\n") if repl != "" else []) + src[i + len(want):]
        return "\n".join(new), None
    if len(hits) > 1:
        return None, (f"edit failed: the find text occurs {len(hits)} times — "
                      "include more surrounding lines so it matches exactly once")
    return None, ("edit failed: the find text was not found (it must match the "
                  "file exactly, including indentation). Look at the exact lines "
                  "first — e.g. grep -n '<a distinctive word>' <file>, or "
                  "sed -n '<start>,<end>p' <file> — then retry with that text.")


# ---- host-side syntax checks ------------------------------------------------
# Every write/edit is syntax-checked HERE, on the router host, from the content
# we already hold: zero model steps, no workspace round-trip, and the verdict
# rides on the write's own observation. Parsing only — ast.parse, node --check,
# bash -n, json.loads never execute anything. Unknown types and missing tools
# say NOTHING (None); a check that cannot run must never read as "OK".
def _node_check(code, ext):
    """'' if node parses it, an error string if not, None if node is absent."""
    node = shutil.which("node")
    if not node:
        return None
    fd, path = tempfile.mkstemp(suffix=ext, prefix="crewe-lint-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            r = subprocess.run([node, "--check", path], capture_output=True,
                               text=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if r.returncode == 0:
        return ""
    err = (r.stderr or "") + (r.stdout or "")
    m = re.search(re.escape(path) + r":(\d+)", err)
    msg = next((l.strip() for l in err.splitlines()
                if "Error" in l and not l.strip().startswith("at ")),
               "syntax error")
    return (f"line {m.group(1)}: " if m else "") + msg[:200]


def _js_check(code, ext=".js"):
    res = _node_check(code, ext)
    if res and ext == ".js":
        # ESM syntax in a .js file is fine in a type:module project; only
        # report a .js that fails BOTH ways
        if _node_check(code, ".mjs") == "":
            return ""
    if res and "Unexpected token '<'" in res:
        # JSX in a .js file (React's App.js) — node cannot parse it and that
        # is not a defect. No verdict beats a false one.
        return None
    if res and ext == ".js" and "import" in res:
        # older node dies at `import` in CJS mode before it ever sees the
        # JSX; look at the ESM attempt's message too
        alt = _node_check(code, ".mjs")
        if alt and "Unexpected token '<'" in alt:
            return None
    return res


# Server-side template syntax inside a page's scripts is not JavaScript yet
_TEMPLATE_MARKERS = ("{{", "{%", "<%", "<?php")


_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
_JS_TYPES = ("", "module", "text/javascript", "application/javascript",
             "text/ecmascript", "application/ecmascript", "javascript")


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
MAX_SCRIPTS_CHECKED = 40


def _blank_html_comments(html):
    spans = [(m.start(2), m.end(2)) for m in _SCRIPT_RE.finditer(html)]
    out, pos = [], 0
    for m in _HTML_COMMENT.finditer(html):
        if any(a <= m.start() < b for a, b in spans):
            continue
        out.append(html[pos:m.start()])
        out.append("\n" * m.group(0).count("\n"))
        pos = m.end()
    out.append(html[pos:])
    return "".join(out)


def _html_check(html):
    """Syntax-check every inline <script> of a page: '' if all parse, an error
    naming the PAGE line if one does not, None if node is absent (or the page
    has more scripts than we are willing to spawn node for)."""
    # commented-out scripts are not code; blank them newline-for-newline so
    # the line numbers of everything after them still hold — but a "<!--"
    # inside a script's own string is code, so comments that START inside a
    # script body are left alone
    html = _blank_html_comments(html)
    checked = 0
    for m in _SCRIPT_RE.finditer(html):
        checked += 1
        if checked > MAX_SCRIPTS_CHECKED:
            return None
        attrs, body = m.group(1), m.group(2)
        if re.search(r"\bsrc\s*=", attrs, re.I) or not body.strip():
            continue
        t = re.search(r"\btype\s*=\s*[\"']?([\w/+.-]+)", attrs, re.I)
        typ = t.group(1).lower() if t else ""
        if typ not in _JS_TYPES:
            continue
        if any(mk in body for mk in _TEMPLATE_MARKERS):
            continue            # a Jinja/EJS/PHP template: not JS until rendered
        res = _js_check(body, ".mjs" if typ == "module" else ".js")
        if res is None:
            return None
        if res:
            start = html[:m.start(2)].count("\n") + 1
            lm = re.match(r"line (\d+): (.*)", res, re.S)
            if lm:
                return (f"line {start + int(lm.group(1)) - 1} (inline <script>): "
                        f"{lm.group(2)}")
            return f"inline <script> starting at line {start}: {res}"
    return ""


def _lint(path, content):
    """(ok, message) for a file the model just wrote, or None when there is
    nothing trustworthy to say (unknown type, tool missing, check crashed)."""
    p = path.lower()
    if not isinstance(content, str) or len(content) > 4_000_000:
        return None
    content = content.lstrip("\ufeff")      # a BOM is not a syntax error
    try:
        if p.endswith(".py"):
            try:
                ast.parse(content, filename=path)
                return True, "syntax OK"
            except SyntaxError as e:
                return False, f"line {e.lineno}: {e.msg}"
        if p.endswith(".json"):
            try:
                json.loads(content)
                return True, "valid JSON"
            except ValueError as e:
                if "//" in content or "/*" in content:
                    return None     # JSONC (tsconfig, eslintrc): comments are allowed there
                return False, str(e)[:200]
        if p.endswith((".js", ".mjs", ".cjs", ".jsx")):
            res = _js_check(content, ".mjs" if p.endswith(".mjs")
                            else ".cjs" if p.endswith(".cjs") else ".js")
        elif p.endswith((".html", ".htm")):
            res = _html_check(content)
        elif p.endswith((".sh", ".bash")):
            sh = shutil.which("bash") or shutil.which("sh")
            if not sh:
                return None
            # -O extglob: `bash -n` does not execute `shopt`, so extended
            # patterns in a perfectly good script read as errors without it
            flags = ["-O", "extglob", "-n"] if sh.endswith("bash") else ["-n"]
            r = subprocess.run([sh, *flags], input=content, capture_output=True,
                               text=True, timeout=15)
            res = "" if r.returncode == 0 else (
                (r.stderr.strip().splitlines() or ["syntax error"])[-1][:200])
        else:
            return None
    except Exception:
        return None
    if res is None:
        return None
    return (True, "syntax OK") if res == "" else (False, res)


def _lint_note(path, content):
    """Observation suffix for a write/edit plus the raw verdict for the
    finish gate: ('', None) when nothing can be said."""
    res = _lint(path, content)
    if res is None:
        return "", None
    ok, msg = res
    if ok:
        return f" — {msg}", res
    return (f"\nSYNTAX ERROR in {path}: {msg}\nFix this before running or "
            "finishing."), res


def _run_hint(paths):
    """What 'verify it' concretely means for the files that changed."""
    for p in paths:
        if p.endswith(".py"):
            return f"e.g. `python3 {p}`, or your test suite"
        if p.endswith((".js", ".mjs", ".cjs")):
            return f"e.g. `node {p}` or `npm test`"
        if p.endswith(".ts"):
            return "e.g. `npx tsc --noEmit` or `npm test`"
        if p.endswith(".sh"):
            return f"e.g. `sh {p}`"
        if p.endswith(".go"):
            return "e.g. `go build ./... && go test ./...`"
        if p.endswith(".rs"):
            return "e.g. `cargo build` or `cargo test`"
    return "run the program, its tests, or at least a compile/syntax check"


# ---- workspace briefing -----------------------------------------------------
# The model used to be told it was in "/workspace inside a sandboxed
# container" no matter which backend ran it — a local-mode run spent its first
# four steps on `cd /workspace` failures. Tell it the truth, and probe the
# tools once up front so it never spends a step on `which node`.
_PROBE_CMD = ("for t in python3 node npm git pip; do "
              "if command -v $t >/dev/null 2>&1; then printf '%s: ' $t; "
              "$t --version 2>&1 | head -1; else echo \"$t: not installed\"; fi; "
              "done")


def _backend_kind(sb):
    k = getattr(sb, "kind", None)
    if k:
        return k
    name = type(sb).__name__.lower()
    for key in ("sandbox", "local", "ssh"):
        if key in name:
            return key
    return "unknown"


def _describe_workspace(sb, network):
    wd = getattr(sb, "workdir", None) or "the workspace directory"
    kind = _backend_kind(sb)
    if kind == "sandbox":
        net = ("package mirrors (pip, npm, apt) and GitHub only"
               if network == "egress" else "none")
        where = (f"{wd} inside an ephemeral Docker container (Debian, "
                 f"non-root). Network: {net}.")
    elif kind == "local":
        where = (f"{wd} on the router's own host machine — NOT sandboxed; "
                 "commands run for real as that user.")
    elif kind == "ssh":
        where = (f"{wd} on a remote machine over SSH — NOT sandboxed; "
                 "commands run for real as that user.")
    else:
        where = f"{wd}."
    return (f"Working directory: {where} Commands run with this as the "
            "current directory; relative paths resolve here.")


def _probe_tools(sb):
    try:
        _code, out, _err = sb.run(_PROBE_CMD, timeout=30)
    except Exception:
        return None
    out = (out or "").strip()
    return out[:600] if out else None



class Audit:
    KEEP = int(os.environ.get("CREWE_AGENT_AUDIT_KEEP", "300"))

    def __init__(self, run_id):
        os.makedirs(AUDIT_DIR, exist_ok=True)
        self.path = os.path.join(AUDIT_DIR, f"{run_id}.jsonl")
        try:    # bounded history — the dir grew without limit before
            logs = sorted((os.path.join(AUDIT_DIR, f) for f in os.listdir(AUDIT_DIR)
                           if f.endswith(".jsonl")), key=os.path.getmtime)
            for old in logs[:-self.KEEP]:
                os.remove(old)
        except OSError:
            pass

    def log(self, **event):
        with open(self.path, "a") as f:
            f.write(json.dumps(event) + "\n")


_GH_TASK_URL = re.compile(r"https?://github\.com/[A-Za-z0-9][\w.-]*/[\w.-]+")


def run_agent(task, *, max_steps=12, network="egress", image=None, on_event=None,
              snapshot_dir=None, should_cancel=None, backend=None, plan=True,
              plan_text=None, seed_dir=None, coder=None, clone=True,
              pull_notes=None):
    """Drive a workspace to complete `task`. Returns a result dict.

    backend:      a workspace object (Sandbox or SSHWorkspace). If None, a Docker
                  Sandbox is created — the loop is identical either way.
    snapshot_dir: if given, the workspace files are copied here before teardown.
    should_cancel: optional callable checked each step; True → stop early.
    plan:         if True, first expand the task into a bounded MVP spec (shown
                  in the transcript) and build to it — helps small models stay
                  scoped. Turn off for literal execution of an exact request.
    coder:        optional {"url","key"?,"model"?} — which model drives the loop
                  (planning included). None → the CREWE_AGENT_URL default.
    clone:        False skips the harness auto-clone of a github URL in the
                  task — for revise rounds whose workspace already holds the
                  (possibly edited) repo; re-cloning would rm -rf those edits.
    pull_notes:   optional callable returning a list of notes the user sent
                  while the run was in progress; drained after every action
                  and delivered with that observation (a note pending when
                  the model calls finish defers the finish by one step)."""
    run_id = uuid.uuid4().hex[:12]
    audit = Audit(run_id)
    # record WHICH coder drove the run — the default coder role points at
    # whatever the backend list says today, so a postmortem can't reconstruct
    # it later. URL + model only; the key must never touch the audit log.
    audit.log(t="start", task=task, max_steps=max_steps, network=network,
              coder={"url": (coder or {}).get("url") or _agent_url(),
                     "model": (coder or {}).get("model")})

    def emit(**e):
        if e.get("t") != "model_progress":   # transient UI signal, not history
            audit.log(**e)
        if on_event:
            on_event(e)

    # planning preamble. plan_text (a user-reviewed/edited spec) wins and is used
    # verbatim; else plan=True generates one; else run the raw task.
    user_task = f"TASK: {task}"
    spec = plan_text.strip() if plan_text and plan_text.strip() else None
    if spec is None and plan:
        try:
            spec = _plan(task, coder=coder, should_cancel=should_cancel)
        except AgentCancelled:
            # stopped during planning — before any container was even created
            result = {"run_id": run_id, "done": False, "summary": None,
                      "steps": 0, "cancelled": True, "files": []}
            emit(t="cancelled", step=0)
            audit.log(t="end", done=False, summary=None, steps=0)
            result["audit"] = audit.path
            return result
        except Exception as e:
            emit(t="plan_error", error=str(e))
    if spec:
        emit(t="plan", spec=spec)
        user_task = (f"TASK: {task}\n\nPLAN — build to this bounded spec "
                     f"(do not exceed its scope):\n{spec}")
    # the model must KNOW its budget or it spends like it has none: the only
    # "~15 actions" hint lived in the planner prompt, which plan=False
    # follow-ups skip by design — one fix run burned all 15 steps diagnosing
    user_task += (f"\n\nBUDGET: you have {max_steps} action(s) in total; each "
                  "observation shows the count. Investigate briefly, make the "
                  "change, verify it, and call finish before they run out.")

    # native tool calling for capable OpenAI-kind coders (e.g. a strong local
    # the text protocol stays the fallback (Claude, small models, a backend
    # that rejects the tools param). One observation per action either way.
    kind = (coder or {}).get("kind", "openai") or "openai"
    use_tools = TOOLS_ENABLED and kind == "openai"
    tools_fell_back = False
    messages = [{"role": "system", "content": TOOL_SYSTEM if use_tools else SYSTEM},
                {"role": "user", "content": user_task}]

    # the history budget follows the coder's REAL window when it is published
    ctx_tokens = (coder or {}).get("ctx_tokens") or _probe_ctx(coder)
    ctx_budget = _ctx_budget(ctx_tokens)
    # a small window also gets smaller observations: one 12 KB log on an 8k
    # model is most of its memory
    obs_limit = min(OBS_LIMIT, max(2000, ctx_budget // 6))
    emit(t="harness", note=(f"coder window: {ctx_tokens:,} tokens"
                            if ctx_tokens else "coder window: unknown")
                           + f" — history budget ≈ {ctx_budget // 1000} KB; "
                             "older turns are condensed past it; observations "
                             f"clipped at {obs_limit // 1000} KB")
    emit(t="harness", note="action channel: native tool calls"
                           if use_tools else "action channel: text protocol")

    if backend is None:
        sb_kwargs = {"network": network}
        if image:
            sb_kwargs["image"] = image
        backend = Sandbox(**sb_kwargs)

    result = {"run_id": run_id, "done": False, "summary": None, "steps": 0}
    with backend as sb:
        # ---- workspace briefing: harness steps, no model turns spent --------
        ws_lines = [_describe_workspace(sb, network)]
        tools = _probe_tools(sb)
        if tools:
            ws_lines.append("Tools available (name: version):\n" + tools)
        primed = []
        listing = []
        if seed_dir:
            if hasattr(sb, "restore") and sb.restore(seed_dir):
                emit(t="harness", note="workspace restored from the previous run")
                try:
                    listing = sb.list_files(limit=80)
                except Exception:
                    listing = []
                primed.append("The workspace already contains the files from the "
                              "previous run (including any edits it made). List "
                              "or read them before assuming their contents. "
                              "This is a follow-up on an existing build: diagnose "
                              "briefly, then make the smallest edit that "
                              "addresses the task, verify it, and finish — do "
                              "not spend the whole budget investigating."
                              + ("\nFiles present:\n" + "\n".join(listing)
                                 if listing else ""))
            else:
                # a continuation that ISN'T continuing must be said out loud —
                # silently starting empty rebuilds from scratch and then
                # overwrites the project's snapshot with the fresh build
                emit(t="harness", note="seed restore FAILED (or backend cannot "
                     "restore) — starting with an EMPTY workspace")
                primed.append("NOTE: restoring the previous run's files FAILED "
                              "— the workspace is EMPTY. Tell the user plainly "
                              "if the task assumed existing files.")
        cloned = False
        m = _GH_TASK_URL.search(task or "") if clone else None
        if m:
            # The harness clones — one model step saved, and no flailing over
            # proxy or --depth flags. A failed clone must be REPORTED, never
            # papered over with an answer from general knowledge.
            url = m.group(0)
            code, out, err = sb.run(
                f"rm -rf repo && git clone --depth 1 {shlex.quote(url)} repo "
                "&& find repo -maxdepth 2 -type f | head -40", timeout=180)
            if code == 0:
                cloned = True
                emit(t="harness", note=f"cloned {url} -> ./repo")
                primed.append(
                    f"The repository {url} is ALREADY CLONED at ./repo "
                    f"(shallow). Its files:\n{out[:2000]}\nRead the real "
                    "files before proposing or making changes; after editing, "
                    "re-read or run them to verify. Work in ./repo.")
            else:
                emit(t="harness", note=f"clone FAILED: {(err or out)[:200]}")
                primed.append(
                    f"NOTE: cloning {url} FAILED ({(err or out)[:300]}). Tell "
                    "the user plainly; do not pretend to have read the repo.")
        elif not seed_dir:
            ws_lines.append("The workspace is empty.")
        # project instructions ride in the briefing: a repo's AGENTS.md is the
        # project's own rulebook (conventions, how to run the tests)
        # a continued project that holds a clone keeps the repo's own rules
        has_repo = cloned or any(str(l).startswith("repo/") for l in listing)
        instr, ipath = _project_instructions(
            sb, roots=("repo/", "") if has_repo else ("",))
        if instr:
            preview = " ".join(instr.split())[:120]
            emit(t="harness", note=f"project instructions loaded from {ipath} "
                                   f"({len(instr)} chars): {preview}…")
            primed.append(f"PROJECT INSTRUCTIONS (from {ipath} — follow them):\n"
                          + instr)
        messages.append({"role": "user",
                         "content": "WORKSPACE STATE:\n"
                                    + "\n\n".join(ws_lines + primed)})
        history_start = len(messages)   # everything before this is never condensed
        condensed = {}                  # message index -> condensation level
        cond_level = 0
        over_warned = False

        # live generation progress, throttled so the event feed stays small
        _last_prog = [0.0]
        def _progress(step):
            def cb(nchars):
                now = time.time()
                if now - _last_prog[0] >= 2:
                    _last_prog[0] = now
                    emit(t="model_progress", step=step, chars=nchars)
            return cb

        seen_reads = {}    # path -> (step, sha256, msg index) of the last `read` served
        seen_runs = {}     # cmd  -> (step, sha256 of its observation)
        changed = {}       # path -> step of a write/edit not yet followed by a run
        lint_bad = {}      # path -> message while its latest content fails the check
        last_change = 0    # step of the most recent write/edit
        last_run = None    # {"step","cmd","exit"} of the most recent run
        parse_fails = 0    # consecutive
        bounces = 0        # finish refusals so far
        run_started = time.time()

        def note_change(path, step, verdict):
            nonlocal last_change
            key = os.path.normpath(path)
            changed[key] = step
            last_change = step
            if verdict is not None:
                if verdict[0]:
                    lint_bad.pop(key, None)
                else:
                    lint_bad[key] = verdict[1]

        def recheck_lint():
            # a flagged file may since have been fixed or removed by a shell
            # command (sed -i, cat > f, a formatter, rm): re-read it from the
            # workspace before holding it against the model
            for key in list(lint_bad)[:8]:
                try:
                    c = sb.read_file(key, max_bytes=EDIT_MAX_BYTES + 1)
                except Exception:
                    continue
                if c is not None and len(c.encode("utf-8", "replace")) > EDIT_MAX_BYTES:
                    c = None        # too big to re-judge: no verdict, not a clipped one
                v = None if c is None else _lint(key, c)
                if c is None or v is None or v[0]:
                    lint_bad.pop(key, None)
                else:
                    lint_bad[key] = v[1]

        def needs_run_now():
            return [p for p in changed if p.lower().endswith(RUNNABLE_EXTS)]

        def drain_notes(step):
            # notes the user sent during the run: delivered with this step's
            # observation, outside the clip, each one shown in the transcript
            if not pull_notes:
                return ""
            try:
                notes = pull_notes() or []
            except Exception:
                return ""
            notes = [str(n).strip()[:NOTE_MAX_CHARS] for n in notes
                     if n is not None and str(n).strip()]
            if not notes:
                return ""
            for n in notes:
                emit(t="note", step=step, text=n[:300])
            return ("\n\nNOTE FROM THE USER (sent while you were working — take "
                    "it into account before your next action):\n"
                    + "\n---\n".join(notes))

        def _do_action(action, step, result_index=None):
            """Execute ONE action (run/write/edit/read) and return its
            observation. Shared by the text protocol and the tool-calling
            path so both get identical execution, syntax checks and loop
            hygiene. `finish` is control flow and stays in the loop."""
            nonlocal last_run
            a = action.get("action")
            _obs_idx = result_index if result_index is not None else len(messages)
            try:
              if a == "run":
                  cmd = action.get("cmd")
                  if not isinstance(cmd, str) or not cmd.strip():
                      obs = "invalid 'run' action: 'cmd' must be a non-empty string"
                  else:
                      code, out, err = sb.run(cmd, timeout=CMD_TIMEOUT)
                      obs = (f"exit_code={code}\n--- stdout ---\n{out}"
                             f"\n--- stderr ---\n{err}")
                      last_run = {"step": step, "cmd": cmd, "exit": code}
                      changed.clear()
                      # the same command with the same result is a loop,
                      # not progress — say so before it repeats again
                      digest = hashlib.sha256(
                          obs.encode("utf-8", "replace")).hexdigest()
                      prev = seen_runs.get(cmd)
                      seen_runs[cmd] = (step, digest)
                      # a FAILING command repeated with nothing changed in
                      # between is a loop; a passing re-run (tests after an
                      # edit) is exactly what we want and gets no nag
                      if (prev and prev[1] == digest and code != 0
                              and prev[0] > last_change):
                          obs += (f"\n(identical to the failure at step "
                                  f"{prev[0]} and nothing changed since — "
                                  "repeating this command changes nothing; "
                                  "change the command or fix the cause first)")
              elif a == "write":
                  path, content = action.get("path"), action.get("content", "")
                  if not isinstance(path, str) or not path.strip():
                      obs = "invalid 'write' action: 'path' must be a non-empty string"
                  else:
                      if not isinstance(content, str):
                          content = json.dumps(content)
                      sb.write_file(path, content)
                      obs = f"wrote {path} ({len(content.splitlines())} lines)"
                      note, verdict = _lint_note(path, content)
                      obs += note
                      note_change(path, step, verdict)
                      if action.get("_truncated"):
                          obs += ("\nWARNING: your reply ended without a "
                                  "closing ``` fence — the generation was "
                                  "probably cut off, so this file may be "
                                  "INCOMPLETE. Check its tail (tail -n 20) "
                                  "or run it; write long files in smaller "
                                  "pieces, or use edit.")
              elif a == "edit":
                  path = action.get("path")
                  find, repl = action.get("find"), action.get("replace", "")
                  if not isinstance(path, str) or not path.strip():
                      obs = "invalid 'edit' action: 'path' must be a non-empty string"
                  elif not isinstance(find, str) or not isinstance(repl, str):
                      obs = "invalid 'edit' action: 'find' and 'replace' must be strings"
                  else:
                      # read past the cap so a too-big file is REFUSED,
                      # never truncated on the way back (read_file clips)
                      content = sb.read_file(path, max_bytes=EDIT_MAX_BYTES + 1)
                      if content is None:
                          obs = (f"edit failed: {path} does not exist — use "
                                 "write to create it")
                      elif len(content.encode("utf-8", "replace")) > EDIT_MAX_BYTES:
                          obs = (f"edit failed: {path} is too large to edit "
                                 "safely — change it with a shell command "
                                 "(sed -i, python) instead")
                      elif "\ufffd" in content:
                          obs = (f"edit failed: {path} is not valid UTF-8 and "
                                 "would be damaged by a round-trip — change "
                                 "it with a shell command (sed -i) instead")
                      else:
                          new, why = _apply_edit(content, find, repl)
                          if new is None:
                              obs = f"{why} [{path}]"
                          else:
                              sb.write_file(path, new)
                              obs = (f"edited {path}: replaced "
                                     f"{len(find.splitlines())} line(s) with "
                                     f"{len(repl.splitlines())} line(s); "
                                     f"file is now {len(new.splitlines())} lines")
                              note, verdict = _lint_note(path, new)
                              obs += note
                              note_change(path, step, verdict)
              elif a == "read":
                  path = action.get("path")
                  if not isinstance(path, str) or not path.strip():
                      obs = "invalid 'read' action: 'path' must be a non-empty string"
                  else:
                      content = sb.read_file(path)
                      if content is None:
                          obs = "(file does not exist)"
                      else:
                          # an identical re-read earns a short note, not
                          # another copy: on an over-OBS_LIMIT file every
                          # full read returns the SAME clipped view, and one
                          # run burned 3 of 15 steps re-reading a 22KB file
                          # hoping to see the middle the clip kept hiding
                          digest = hashlib.sha256(
                              content.encode("utf-8", "replace")).hexdigest()
                          prev = seen_reads.get(path)
                          # the observation a served read becomes is
                          # appended at len(messages); if that turn has
                          # since been condensed the content is NOT "still
                          # above" — serve it again instead of pointing at
                          # a stub. A served note keeps pointing at the
                          # REAL copy (the note itself is never condensed).
                          if (prev and prev[1] == digest
                                  and prev[2] not in condensed):
                              obs = (f"(unchanged — you already read {path} "
                                     f"at step {prev[0]} and its content is "
                                     "identical; that read is still above. "
                                     "To see a part the clip omitted, use "
                                     "grep or sed on a line range instead.)")
                              # latest step for the wording, the index of
                              # the real copy for the condensation check
                              seen_reads[path] = (step, digest, prev[2])
                          else:
                              seen_reads[path] = (step, digest, _obs_idx)
                              obs = content
              else:
                  obs = f"unknown action '{a}'. Valid: run, write, edit, read, finish."
            except Exception as e:
              obs = f"action failed: {e}"
            return obs

        for step in range(1, max_steps + 1):
            result["steps"] = step
            if should_cancel and should_cancel():
                result["cancelled"] = True
                emit(t="cancelled", step=step)
                break
            if RUN_MAX_SECS and time.time() - run_started > RUN_MAX_SECS:
                result["timed_out"] = True
                emit(t="warn", step=step,
                     note=f"run time cap reached (CREWE_AGENT_RUN_MAX="
                          f"{RUN_MAX_SECS}s) — stopping with what exists")
                break
            n_new, lvl, total = _condense_history(messages, history_start,
                                                  condensed, ctx_budget)
            if n_new and lvl > cond_level:
                cond_level = lvl
                emit(t="harness", step=step,
                     note=f"context: condensed {len(condensed)} older turn(s)"
                          + (" — window tight, only the last 2 turns stay verbatim"
                             if lvl >= 2 else "")
                          + f" (history ≈ {total // 1000} KB, budget "
                            f"{ctx_budget // 1000} KB)")
            if total > ctx_budget and not over_warned:
                # everything condensable is condensed and it STILL does not
                # fit: say so once — this is the shape of the HTTP 400s this
                # feature exists to prevent
                over_warned = True
                emit(t="warn", step=step,
                     note=f"context: the history is ≈ {total // 1000} KB against "
                          f"a {ctx_budget // 1000} KB budget even after "
                          "condensing — the newest turns alone may not fit the "
                          "coder's window; keep command output short "
                          "(head/tail/grep) and write big files in pieces")
            try:
                reply = _chat(messages, coder=coder, should_cancel=should_cancel,
                              on_progress=_progress(step),
                              tools=AGENT_TOOLS if use_tools else None)
            except AgentCancelled:
                result["cancelled"] = True
                emit(t="cancelled", step=step)
                break
            except requests.exceptions.HTTPError as e:
                st = getattr(e.response, "status_code", None)
                # a backend that does not accept the `tools` param (older
                # llama.cpp, no --jinja): drop to the text protocol for the
                # rest of the run instead of dying
                _low = str(e).lower()
                _tools_rejected = (st == 400 and ("tool" in _low or "function" in _low)
                    and any(w in _low for w in (
                        "not support", "unsupport", "unknown", "unrecogni",
                        "unexpected", "not enabled", "disable", "no tools",
                        "does not accept", "not allowed", "invalid parameter")))
                if use_tools and _tools_rejected and not tools_fell_back:
                    tools_fell_back = True
                    use_tools = False
                    messages[0] = {"role": "system", "content": SYSTEM}
                    emit(t="harness", step=step,
                         note="this backend rejected tool calling — using the "
                              "text protocol for the rest of the run")
                    continue
                emit(t="model_error", step=step, error=str(e))
                result["error"] = f"model error: {e}"
                break
            except Exception as e:
                emit(t="model_error", step=step, error=str(e))
                result["error"] = f"model error: {e}"
                break

            # normalise: tool mode returns {content, tool_calls}; text mode a str
            if isinstance(reply, dict):
                tool_calls = reply.get("tool_calls") or []
                reply_text = reply.get("content") or ""
            else:
                tool_calls, reply_text = None, reply

            if tool_calls:
                # ---- native tool-calling step: execute 1..N calls ----------
                # One model round-trip is one step even when it batches calls.
                # Every tool_call id gets exactly one result (OpenAI requires
                # it); ids are de-duplicated; at most ONE finish is evaluated
                # per step; a note or an accepted finish ends the batch and the
                # remaining calls get a stub result so the history stays valid.
                parse_fails = 0
                norm, seen_ids = [], set()
                for i, c in enumerate(tool_calls):
                    cid = c.get("id")
                    if not cid or cid in seen_ids:
                        cid = f"call_{step}_{i}"
                    seen_ids.add(cid)
                    norm.append({"id": cid, "type": "function",
                                 "function": {"name": c.get("name") or "",
                                              "arguments": c.get("arguments") or "{}"}})
                messages.append({"role": "assistant",
                                 "content": reply_text or None,
                                 "tool_calls": norm})
                results = []          # (tool_call_id, obs) in call order
                finished = deferred = finish_seen = False
                for i, c in enumerate(tool_calls):
                    cid = norm[i]["id"]
                    action, err = _toolcall_to_action(c)
                    a = action.get("action")
                    emit(t="action", step=step, action=a,
                         detail={k: (v[:200] if isinstance(v, str) else v)
                                 for k, v in action.items()
                                 if k != "action" and not k.startswith("_")})
                    if err:
                        results.append((cid, err))
                        continue
                    if a == "finish":
                        # only ONE finish decision per step — a batch of
                        # finishes must not burn the bounce budget or slip
                        # past the gate without a real verify turn in between
                        if finish_seen:
                            results.append((cid, "you already tried to finish "
                                "this turn — act on the feedback above, then "
                                "call finish once."))
                            continue
                        finish_seen = True
                        left = max_steps - step
                        notes_txt = drain_notes(step) if left >= 2 else ""
                        if notes_txt:
                            emit(t="warn", step=step,
                                 note="finish deferred: a note from the user "
                                      "arrived during the run")
                            results.append((cid, "NOT FINISHED — a note from "
                                "the user arrived while you were working. Act "
                                "on it, verify, then call finish again (if it "
                                "changes nothing, say so in your summary)."
                                + notes_txt))
                            deferred = True
                            break
                        problems = []
                        recheck_lint()
                        if lint_bad:
                            problems.append("these files still have SYNTAX "
                                "ERRORS: " + "; ".join(f"{p} ({m})" for p, m
                                in list(lint_bad.items())[:4]))
                        needs_run = needs_run_now()
                        if needs_run:
                            problems.append(f"you changed {', '.join(needs_run[:6])} "
                                            "and have not run anything since")
                        if problems and bounces < MAX_FINISH_BOUNCES and left >= 2:
                            bounces += 1
                            emit(t="warn", step=step,
                                 note="finish refused: " + "; ".join(problems))
                            results.append((cid, "NOT FINISHED — finish refused: "
                                + "; ".join(problems) + ". Verify first ("
                                + _run_hint(needs_run or list(changed))
                                + "), read the output, fix what fails, then "
                                "call finish and say what you ran."))
                            continue
                        result["done"] = True
                        result["summary"] = action.get("summary", "")
                        results.append((cid, "finishing."))
                        finished = True
                        break
                    # the observation lands after the assistant turn and any
                    # results already collected this step
                    results.append((cid, _do_action(
                        action, step, len(messages) + len(results))))
                # every listed tool_call MUST get a result or the re-sent
                # history is invalid — stub the ones a note/finish cut short
                answered = {cid for cid, _ in results}
                for nm in norm:
                    if nm["id"] not in answered:
                        results.append((nm["id"], "(not executed — the turn "
                                        "ended before this call ran)"))
                for cid, obs in results:
                    emit(t="observation", step=step, obs=str(obs)[:600])
                    messages.append({"role": "tool", "tool_call_id": cid,
                                     "content": _clip_obs(str(obs), obs_limit)})
                if finished:
                    break
                if deferred:
                    # like the text path's finish-defer: re-enter on a fresh
                    # step, no trailer (the note was already delivered)
                    continue
                # budget + notes trailer (mirrors the text OBSERVATION wrapper)
                left = max_steps - step
                trailer = f"(step {step}/{max_steps} — {left} action(s) left)"
                if 0 < left <= 3:
                    trailer += ("\n[commit now: make your best fix, verify it "
                                "quickly, and call finish.]")
                trailer += drain_notes(step) if step < max_steps else ""
                messages.append({"role": "user", "content": trailer})
                continue

            # ---- text protocol (unchanged) --------------------------------
            reply = reply_text
            messages.append({"role": "assistant", "content": reply})
            action = _parse_action(reply)
            if not action:
                parse_fails += 1
                emit(t="parse_fail", step=step, reply=reply[:400])
                if parse_fails >= MAX_PARSE_FAILS:
                    result["error"] = (f"model produced {parse_fails} "
                                       "unparseable replies in a row")
                    emit(t="model_error", step=step, error=result["error"])
                    break
                more = MAX_PARSE_FAILS - parse_fails
                messages.append({"role": "user", "content":
                    f"That was not a valid action (step {step}/{max_steps} "
                    "spent). Reply with exactly one action: a ```json block "
                    'such as {"action":"run","cmd":"ls"}, or a raw WRITE:/EDIT: '
                    "block — and nothing else."
                    + (f" The run stops after {more} more unparseable "
                       "repl" + ("y." if more == 1 else "ies.")
                       if parse_fails >= 2 else "")})
                continue
            parse_fails = 0

            a = action.get("action")
            emit(t="action", step=step, action=a,
                 detail={k: (v[:200] if isinstance(v, str) else v)
                         for k, v in action.items()
                         if k != "action" and not k.startswith("_")})
            extra = _count_actions(reply) - 1
            prefix = (f"(your reply contained {extra + 1} actions — only the "
                      "FIRST was executed; send ONE action per reply)\n"
                      if extra > 0 else "")

            if a == "finish":
                # a note the user sent meanwhile outranks a finish: deliver it
                # and let the model act (or finish again, if it changes
                # nothing). Only with two steps to spare — one step cannot
                # both act and finish, and a finishing run must never become
                # an exhausted one; a note that cannot be delivered is
                # reported at the end instead of silently dropped.
                left = max_steps - step
                notes_txt = drain_notes(step) if left >= 2 else ""
                if notes_txt:
                    emit(t="warn", step=step,
                         note="finish deferred: a note from the user arrived "
                              "during the run")
                    wrap = (f"\n\n[{left} action(s) left — commit now: make "
                            "your best fix, verify it quickly, and call finish.]"
                            if left <= 3 else "")
                    messages.append({"role": "user", "content":
                        f"OBSERVATION (step {step}/{max_steps}):\nNOT FINISHED "
                        "— a note from the user arrived while you were working. "
                        "Act on it, verify, then call finish again (if it "
                        "changes nothing, say so in your finish summary)."
                        + notes_txt + wrap})
                    continue
                # ---- the finish gate: evidence before claims ---------------
                # Deterministic and bounded — no critic model, no rounds. It
                # only asks for what the rules already say: run what you
                # changed, fix what the syntax check flagged.
                left = max_steps - step
                problems = []
                recheck_lint()
                if lint_bad:
                    problems.append(
                        "these files still have SYNTAX ERRORS: "
                        + "; ".join(f"{p} ({m})" for p, m
                                    in list(lint_bad.items())[:4]))
                needs_run = needs_run_now()
                if needs_run:
                    problems.append(f"you changed {', '.join(needs_run[:6])} "
                                    "and have not run anything since")
                if problems and bounces < MAX_FINISH_BOUNCES and left >= 2:
                    bounces += 1
                    obs = ("NOT FINISHED — finish refused: "
                           + "; ".join(problems) + ". Verify first ("
                           + _run_hint(needs_run or list(changed))
                           + "), read the output, fix what fails, then call "
                           "finish and say what you ran.")
                    emit(t="warn", step=step,
                         note="finish refused: " + "; ".join(problems))
                else:
                    result["done"] = True
                    result["summary"] = action.get("summary", "")
                    break
            else:
                obs = _do_action(action, step)

            obs = prefix + obs
            emit(t="observation", step=step, obs=obs[:600])
            # counter + warning ride OUTSIDE the clip so they can never be
            # trimmed away with the observation's middle
            left = max_steps - step
            wrap = ""
            if 0 < left <= 3:
                wrap = (f"\n\n[{left} action(s) left — commit now: make your "
                        "best fix, verify it quickly, and call finish.]")
            # on the last step no model call follows: leave notes queued so
            # they are reported as undelivered rather than "delivered"
            notes_txt = drain_notes(step) if step < max_steps else ""
            messages.append({"role": "user", "content":
                f"OBSERVATION (step {step}/{max_steps}):"
                f"\n{_clip_obs(obs, obs_limit)}{notes_txt}{wrap}"})

        # ---- notes that arrived too late are said out loud ------------------
        if pull_notes:
            try:
                late = [str(n).strip() for n in (pull_notes() or [])
                        if n is not None and str(n).strip()]
            except Exception:
                late = []
            if late:
                result["undelivered_notes"] = len(late)
                emit(t="warn", note=f"{len(late)} note(s) arrived too late to "
                     "reach the agent: " + " | ".join(n[:80] for n in late[:3])
                     + (" …" if len(late) > 3 else ""))

        # ---- verification verdict: what the evidence supports ---------------
        # None = nothing was changed, so nothing to verify (an answer-only run).
        verified, vnote = None, None
        if last_change:
            recheck_lint()
            needs_run = needs_run_now()
            if lint_bad:
                verified = False
                vnote = ("syntax errors outstanding in "
                         + ", ".join(list(lint_bad)[:4]))
            elif needs_run:
                verified = False
                vnote = ("nothing was run after the last change to "
                         + ", ".join(needs_run[:4]))
            elif last_run and last_run["step"] >= last_change:
                if last_run["exit"] == 0:
                    verified = True
                    vnote = (f"`{last_run['cmd'][:80]}` exited 0 after the "
                             "last change")
                else:
                    verified = False
                    vnote = (f"the last command after the last change exited "
                             f"{last_run['exit']}: `{last_run['cmd'][:80]}`")
            else:
                verified = True
                vnote = ("only static files changed; syntax checks passed "
                         "where available")
        result["verified"], result["verify_note"] = verified, vnote
        result["condensed"] = len(condensed)
        result["ctx_tokens"] = ctx_tokens
        if last_run:
            result["last_run"] = {"cmd": last_run["cmd"][:200],
                                  "exit": last_run["exit"]}
        if verified is not None:
            emit(t="harness" if verified else "warn",
                 note=("verification: ✓ " if verified else "verification: ⚠ ")
                      + vnote)

        if (not result["done"] and not result.get("cancelled")
                and "error" not in result):
            # ran out of steps without `finish` — the UI must not call this
            # "done": the build is best-effort at whatever step 15 left behind
            result["exhausted"] = True
            if not result.get("timed_out"):
                emit(t="warn", note=f"step budget ({max_steps}) exhausted before finish")

        # capture the final workspace file list + snapshot it out — both via the
        # backend interface, so this works for Docker and SSH identically.
        result["files"] = sb.list_files()
        if snapshot_dir:
            try:
                sb.snapshot(snapshot_dir)
                result["snapshot_dir"] = snapshot_dir
            except Exception as e:
                emit(t="snapshot_error", error=str(e))

    audit.log(t="end", **{k: result.get(k) for k in
                          ("done", "summary", "steps", "verified", "verify_note")})
    result["audit"] = audit.path
    return result


if __name__ == "__main__":
    import sys
    task = sys.argv[1] if len(sys.argv) > 1 else (
        "Create fib.py that prints the first 15 Fibonacci numbers on one line, "
        "run it, and finish with the exact output.")
    print(f"→ task: {task}\n")
    res = run_agent(task, max_steps=int(os.environ.get("MAX_STEPS", 10)),
                    on_event=lambda e: print(f"  [{e.get('t')}] " + (
                        f"{e.get('action')} {json.dumps(e.get('detail',{}))[:100]}"
                        if e.get("t") == "action" else
                        (e.get("obs","")[:120] if e.get("t")=="observation" else
                         json.dumps({k:v for k,v in e.items() if k not in ('t',)})[:120]))))
    print("\n--- result ---")
    print(json.dumps({k: res[k] for k in ("run_id","done","summary","steps","files")},
                     indent=2))
    print("audit:", res["audit"])
