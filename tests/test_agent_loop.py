"""Offline regression for agent.run_agent after the Blender removal.

Stubs the model (no network) and the workspace backend, then drives the full
loop: plan → write → run → read → finish, plus snapshot and cancel. Proves the
loop still works with the `mode`/`model_url` plumbing gone.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import agent

ok, bad = [], []


def check(name, cond):
    (ok if cond else bad).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")


class StubBackend:
    """Minimal workspace: enough surface for the loop (no Docker, no SSH)."""

    def __init__(self):
        self.files = {}
        self.ran = []
        self.snapshotted = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cmd, timeout=None):
        self.ran.append(cmd)
        return 0, f"ran:{cmd}", ""

    def write_file(self, path, content):
        self.files[path] = content

    def read_file(self, path, max_bytes=200_000):
        return self.files.get(path)

    def list_files(self, limit=200):
        return sorted(self.files)

    def snapshot(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        self.snapshotted = dest_dir
        return dest_dir


def scripted(replies):
    """Replace agent._chat with a canned sequence; record the system prompt."""
    seen = {"systems": [], "calls": 0}

    def fake_chat(messages, **kw):
        assert "url" not in kw, "model_url plumbing should be gone"
        seen["systems"].append(messages[0]["content"])
        seen["calls"] += 1
        return replies[min(seen["calls"] - 1, len(replies) - 1)]

    agent._chat = fake_chat
    return seen


print("agent loop (offline, stubbed model + backend)")

# --- 1. full happy path: plan, write, run, read, finish ----------------------
seen = scripted([
    "Scope: a tiny thing\nFeatures:\n- one\nAcceptance:\n- runs",   # the plan
    "WRITE: hello.py\n```\nprint('hi')\n```",
    '```json\n{"action":"run","cmd":"python3 hello.py"}\n```',
    '```json\n{"action":"read","path":"hello.py"}\n```',
    '```json\n{"action":"finish","summary":"built it"}\n```',
])
sb = StubBackend()
with tempfile.TemporaryDirectory() as snap:
    res = agent.run_agent("make hello", max_steps=8, backend=sb,
                          snapshot_dir=os.path.join(snap, "run1"))
    check("finishes cleanly", res["done"] is True)
    check("carries the summary", res["summary"] == "built it")
    check("wrote the file", sb.files.get("hello.py") == "print('hi')")
    check("ran the command", "python3 hello.py" in sb.ran)
    check("reports files", res["files"] == ["hello.py"])
    check("snapshots the workspace", res.get("snapshot_dir") == sb.snapshotted)
    check("planned first (extra model call)", seen["calls"] == 5)
    check("uses the CODE system prompt", seen["systems"][1] == agent.SYSTEM)
    check("uses the CODE planner prompt", seen["systems"][0] == agent.PLANNER_SYSTEM)
    check("plan reached the task", "Scope: a tiny thing" in
          open(res["audit"]).read())

# --- 2. finish is immediate — no vision/render round-trip --------------------
seen = scripted(['```json\n{"action":"finish","summary":"done"}\n```'])
sb = StubBackend()
res = agent.run_agent("anything", max_steps=5, backend=sb, plan=False)
check("finish takes effect on step 1", res["steps"] == 1 and res["done"])
check("no extra model calls after finish", seen["calls"] == 1)
check("no render script written", sb.files == {})
check("no blender invoked", not any("blender" in c for c in sb.ran))

# --- 3. plan_text is used verbatim, no planning call -------------------------
seen = scripted(['```json\n{"action":"finish","summary":"ok"}\n```'])
res = agent.run_agent("t", max_steps=3, backend=StubBackend(),
                      plan_text="Scope: user-edited spec")
check("plan_text skips the planner call", seen["calls"] == 1)
check("plan_text reaches the model", "user-edited spec" in
      open(res["audit"]).read())

# --- 4. cancellation still works --------------------------------------------
seen = scripted(['```json\n{"action":"run","cmd":"sleep"}\n```'])
res = agent.run_agent("t", max_steps=5, backend=StubBackend(), plan=False,
                      should_cancel=lambda: True)
check("cancels before acting", res.get("cancelled") is True and not res["done"])

# --- 5. removed symbols are actually gone -----------------------------------
for sym in ("BLENDER_SYSTEM", "BLENDER_PLANNER", "RENDER_APPEND", "VISION_URL",
            "MAX_VISION_ROUNDS", "_render_and_critique", "_vision_critic_prompt",
            "_vision_chat", "_SYSTEMS", "_PLANNERS"):
    check(f"agent.{sym} removed", not hasattr(agent, sym))

import inspect
sig = inspect.signature(agent.run_agent).parameters
check("run_agent has no mode=", "mode" not in sig)
check("run_agent has no model_url=", "model_url" not in sig)
check("_plan takes task + coder + cancel", list(inspect.signature(agent._plan).parameters) == ["task", "coder", "should_cancel"])

import sandbox
check("Sandbox.read_bytes removed", not hasattr(sandbox.Sandbox, "read_bytes"))
check("Sandbox.read_file kept", hasattr(sandbox.Sandbox, "read_file"))

print(f"\n{len(ok)} passed, {len(bad)} failed")
raise SystemExit(1 if bad else 0)
