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
import json
import os
import re
import shlex
import subprocess
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


class AgentCancelled(Exception):
    """Raised inside _chat when the caller's should_cancel fires mid-stream —
    lets Stop land within a chunk instead of after a whole generation."""


def _agent_url():
    # read per call, not at import time, so setting CREWE_AGENT_URL after this
    # module is first imported (e.g. by a long-lived router process) still works
    return os.environ.get("CREWE_AGENT_URL", DEFAULT_AGENT_URL)

SYSTEM = """You are a coding agent working inside a sandboxed Linux container
(Debian, with node, python3, pip, npm, git, build-essential). You complete the
task by taking ONE action at a time and observing the result.

Reply with EXACTLY ONE action as a JSON object in a ```json fenced block, and
nothing else. The actions are:

  {"action":"run",   "cmd":"<shell command>"}      run a shell command in /workspace
  {"action":"write", "path":"<file>", "content":"<file contents>"}   write a file
  {"action":"read",  "path":"<file>"}              read a file back
  {"action":"finish","summary":"<what you built>"} you are done

For writing files, PREFER this token-efficient form (no JSON escaping needed —
put the file's RAW contents between the fences):

  WRITE: path/to/file.py
  ```
  <raw file contents, exactly as they should appear on disk>
  ```

Rules:
- After each action you get an OBSERVATION. Use it. Do not imagine results.
- Install packages you need with pip/npm — the sandbox allows package mirrors.
- VERIFY your work by running it before you finish.
- Keep files focused. Prefer editing to rewriting.
- One JSON action per reply. No prose outside the json block.

Example reply:
```json
{"action":"write","path":"hello.py","content":"print('hi')\\n"}
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


def _chat(messages, temperature=0.2, max_tokens=8192, timeout=None, coder=None,
          should_cancel=None, on_progress=None):
    """One coder call, STREAMED.

    coder: optional {"url","key"?,"model"?} choosing which model drives the
    loop (the router resolves it server-side from its backend list); default
    is the CREWE_AGENT_URL / conventional-local coder.
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
    url = coder.get("url") or _agent_url()
    headers = {"Authorization": f"Bearer {coder['key']}"} if coder.get("key") else {}
    body = {
        "messages": messages, "temperature": temperature,
        "max_tokens": max_tokens, "stream": True,
        # Thinking OFF: the ReAct loop IS the reasoning (act→observe→repeat), and
        # a thinking model's reasoning tokens were eating the output budget and
        # truncating big writes (a whole-file write would arrive cut off). Same
        # lesson the code pipeline learned. Harmless kwarg on models that ignore it.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if coder.get("model"):
        body["model"] = coder["model"]
    for attempt in (0, 1):
        try:
            return _stream_once(url, headers, body, timeout or (10, READ_TIMEOUT),
                                should_cancel, on_progress)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError):
            if attempt:
                raise
            time.sleep(3)


def _stream_once(url, headers, body, timeout, should_cancel, on_progress):
    started = time.time()
    with requests.post(url, timeout=timeout, headers=headers, json=body,
                       stream=True) as r:
        r.raise_for_status()
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
            try:
                msg = data["choices"][0]["message"]
            except (KeyError, IndexError, TypeError):
                raise RuntimeError(f"coder reply missing choices: {str(data)[:200]}")
            return _strip_think(msg.get("content") or "")
        # byte-level reads, lines assembled by hand: iter_lines buffers up to
        # its chunk_size before yielding, which held cancel checks hostage for
        # seconds on a slow stream. A generation is ≤ ~100KB, so per-byte
        # iteration costs nothing and cancel/timeout react at wire speed.
        parts, buf = [], b""
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
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0].get("delta", {})
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            piece = delta.get("content") or ""
            if piece:
                parts.append(piece)
                n += len(piece)
                if on_progress:
                    on_progress(n)
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


def _parse_action(text):
    """Pull the first valid action out of the reply. Handles the raw WRITE form
    (token-efficient, no escaping) first, then JSON, tolerating prose/fencing."""
    # raw write:  WRITE: <path>\n```\n<raw content>\n```  — checked before JSON
    # because its content is arbitrary code that must NOT be JSON-parsed.
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
    # and the agent can run/fix it, instead of dead-looping on parse_fail.
    m = re.search(r"(?im)^[ \t]*WRITE:[ \t]*(\S+)[ \t]*\r?\n```[\w.\-]*\r?\n(.+)",
                  text, flags=re.DOTALL)
    if m:
        content = re.sub(r"\r?\n?```\s*$", "", m.group(2))
        return {"action": "write", "path": m.group(1).strip(), "content": content}
    # prefer fenced blocks
    blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    candidates = blocks + [text]
    for c in candidates:
        c = c.strip()
        # try the whole thing, then the first {...} span
        for span in ([c] + re.findall(r"\{.*\}", c, flags=re.DOTALL)):
            try:
                obj = json.loads(span)
                if isinstance(obj, dict) and "action" in obj:
                    return obj
            except json.JSONDecodeError:
                continue
    return None


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
              plan_text=None, seed_dir=None, coder=None):
    """Drive a workspace to complete `task`. Returns a result dict.

    backend:      a workspace object (Sandbox or SSHWorkspace). If None, a Docker
                  Sandbox is created — the loop is identical either way.
    snapshot_dir: if given, the workspace files are copied here before teardown.
    should_cancel: optional callable checked each step; True → stop early.
    plan:         if True, first expand the task into a bounded MVP spec (shown
                  in the transcript) and build to it — helps small models stay
                  scoped. Turn off for literal execution of an exact request.
    coder:        optional {"url","key"?,"model"?} — which model drives the loop
                  (planning included). None → the CREWE_AGENT_URL default."""
    run_id = uuid.uuid4().hex[:12]
    audit = Audit(run_id)
    audit.log(t="start", task=task, max_steps=max_steps, network=network)

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

    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_task}]

    if backend is None:
        sb_kwargs = {"network": network}
        if image:
            sb_kwargs["image"] = image
        backend = Sandbox(**sb_kwargs)

    result = {"run_id": run_id, "done": False, "summary": None, "steps": 0}
    with backend as sb:
        # ---- workspace priming: harness steps, no model turns spent --------
        primed = []
        if seed_dir:
            if hasattr(sb, "restore") and sb.restore(seed_dir):
                emit(t="harness", note="workspace restored from the previous run")
                primed.append("The workspace already contains the files from the "
                              "previous run (including any edits it made). List "
                              "or read them before assuming their contents.")
            else:
                # a continuation that ISN'T continuing must be said out loud —
                # silently starting empty rebuilds from scratch and then
                # overwrites the project's snapshot with the fresh build
                emit(t="harness", note="seed restore FAILED (or backend cannot "
                     "restore) — starting with an EMPTY workspace")
                primed.append("NOTE: restoring the previous run's files FAILED "
                              "— the workspace is EMPTY. Tell the user plainly "
                              "if the task assumed existing files.")
        m = _GH_TASK_URL.search(task or "")
        if m:
            # The harness clones — one model step saved, and no flailing over
            # proxy or --depth flags. A failed clone must be REPORTED, never
            # papered over with an answer from general knowledge.
            url = m.group(0)
            code, out, err = sb.run(
                f"rm -rf repo && git clone --depth 1 {shlex.quote(url)} repo "
                "&& find repo -maxdepth 2 -type f | head -40", timeout=180)
            if code == 0:
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
        if primed:
            messages.append({"role": "user",
                             "content": "WORKSPACE STATE:\n" + "\n\n".join(primed)})

        # live generation progress, throttled so the event feed stays small
        _last_prog = [0.0]
        def _progress(step):
            def cb(nchars):
                now = time.time()
                if now - _last_prog[0] >= 2:
                    _last_prog[0] = now
                    emit(t="model_progress", step=step, chars=nchars)
            return cb

        for step in range(1, max_steps + 1):
            result["steps"] = step
            if should_cancel and should_cancel():
                result["cancelled"] = True
                emit(t="cancelled", step=step)
                break
            try:
                reply = _chat(messages, coder=coder, should_cancel=should_cancel,
                              on_progress=_progress(step))
            except AgentCancelled:
                result["cancelled"] = True
                emit(t="cancelled", step=step)
                break
            except Exception as e:
                emit(t="model_error", step=step, error=str(e))
                result["error"] = f"model error: {e}"
                break
            messages.append({"role": "assistant", "content": reply})
            action = _parse_action(reply)
            if not action:
                emit(t="parse_fail", step=step, reply=reply[:400])
                messages.append({"role": "user", "content":
                    "That was not a valid action. Reply with exactly one JSON "
                    "action inside a ```json block."})
                continue

            a = action.get("action")
            emit(t="action", step=step, action=a,
                 detail={k: (v[:200] if isinstance(v, str) else v)
                         for k, v in action.items() if k != "action"})

            if a == "finish":
                result["done"] = True
                result["summary"] = action.get("summary", "")
                break
            # Every action failure — wrong field type, backend hiccup, docker
            # timeout — becomes an OBSERVATION the model can react to. An
            # exception here used to unwind the loop and destroy the whole
            # workspace over one bad step.
            try:
                if a == "run":
                    cmd = action.get("cmd")
                    if not isinstance(cmd, str) or not cmd.strip():
                        obs = "invalid 'run' action: 'cmd' must be a non-empty string"
                    else:
                        code, out, err = sb.run(cmd, timeout=CMD_TIMEOUT)
                        obs = (f"exit_code={code}\n--- stdout ---\n{out}"
                               f"\n--- stderr ---\n{err}")
                elif a == "write":
                    path, content = action.get("path"), action.get("content", "")
                    if not isinstance(path, str) or not path.strip():
                        obs = "invalid 'write' action: 'path' must be a non-empty string"
                    else:
                        sb.write_file(path, content if isinstance(content, str)
                                      else json.dumps(content))
                        obs = f"wrote {path}"
                elif a == "read":
                    path = action.get("path")
                    if not isinstance(path, str) or not path.strip():
                        obs = "invalid 'read' action: 'path' must be a non-empty string"
                    else:
                        content = sb.read_file(path)
                        obs = content if content is not None else "(file does not exist)"
                else:
                    obs = f"unknown action '{a}'. Valid: run, write, read, finish."
            except Exception as e:
                obs = f"action failed: {e}"

            emit(t="observation", step=step, obs=obs[:600])
            messages.append({"role": "user",
                             "content": f"OBSERVATION:\n{_clip_obs(obs)}"})

        if (not result["done"] and not result.get("cancelled")
                and "error" not in result):
            # ran out of steps without `finish` — the UI must not call this
            # "done": the build is best-effort at whatever step 15 left behind
            result["exhausted"] = True
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

    audit.log(t="end", **{k: result.get(k) for k in ("done", "summary", "steps")})
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
