"""Offline regression for the step-budget awareness fixes in agent.run_agent.

Born from a real incident: a 15-step fix run ("Pressing I does not do
anything") spent its entire budget diagnosing — four full reads of a 22KB
index.html (each clipped identically by OBS_LIMIT), zero writes, no finish.
These tests pin the countermeasures:

  1. the initial task message states the budget
  2. every observation carries a step N/M counter (outside the clip)
  3. a commit-now warning appears when <=3 actions remain, not before
  4. an identical re-read returns a short note, not another clipped copy
  5. a seeded (fix/continue) workspace primes a bias-to-action instruction
  6. the audit start event records the coder (url+model), never the key

Stubs the model and the backend — no network, no Docker. The audit dir is
redirected to a temp path before agent is imported.
"""
import json
import os
import sys
import tempfile

_AUDIT_TMP = tempfile.mkdtemp(prefix="crewe-audit-test-")
os.environ["CREWE_AGENT_AUDIT_DIR"] = _AUDIT_TMP

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)
import agent

ok, bad = [], []


def check(name, cond):
    (ok if cond else bad).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")


class StubBackend:
    """Minimal workspace surface (mirrors test_agent_loop's stub)."""

    def __init__(self, files=None, restorable=False):
        self.files = dict(files or {})
        self.ran = []
        self.restorable = restorable
        self.restored_from = None

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

    def restore(self, seed_dir):
        self.restored_from = seed_dir
        return self.restorable


def scripted(replies):
    """Replace agent._chat with canned replies; capture every messages list."""
    seen = {"calls": [], "n": 0}

    def fake_chat(messages, **kw):
        seen["calls"].append([dict(m) for m in messages])
        seen["n"] += 1
        return replies[min(seen["n"] - 1, len(replies) - 1)]

    agent._chat = fake_chat
    return seen


def observations(msgs):
    return [m["content"] for m in msgs if m["role"] == "user"
            and m["content"].startswith("OBSERVATION")]


print("agent step-budget awareness (offline, stubbed model + backend)")

# --- 1+2+3. budget line, per-step counter, commit-now warning ----------------
# 8 steps of `run`, then finish on the last call. The messages the model saw
# on its final call contain every observation from the run.
seen = scripted(['```json\n{"action":"run","cmd":"true"}\n```'] * 7
                + ['```json\n{"action":"finish","summary":"done"}\n```'])
res = agent.run_agent("fix the thing", max_steps=8, backend=StubBackend(),
                      plan=False)
final_msgs = seen["calls"][-1]
task_msg = final_msgs[1]["content"]
obs = observations(final_msgs)
check("budget stated in the task message",
      "BUDGET: you have 8 action(s)" in task_msg)
check("run finished (regression: loop still works)", res["done"] is True)
check("every observation carries step N/M",
      len(obs) == 7 and all(f"(step {i}/8):" in obs[i - 1]
                            for i in range(1, 8)))
check("no warning while >3 actions remain",
      not any("commit now" in o for o in obs[:4]))     # steps 1-4: left 7..4
check("warning appears once <=3 remain",
      all("action(s) left — commit now" in o for o in obs[4:7]))
check("warning names the true remainder",
      "[3 action(s) left" in obs[4] and "[2 action(s) left" in obs[5]
      and "[1 action(s) left" in obs[6])

# --- 4. identical re-read -> note, not another clipped copy ------------------
# Scale-realistic: a 22KB file (over OBS_LIMIT=12000), read four times like
# the incident run, with a write in between to prove changed content re-reads
# in full.
big = ("<!DOCTYPE html>\n" + "x" * 11_000 + "\nKEYDOWN_HANDLER_HERE\n"
       + "y" * 11_000 + "\n</html>\n")
reads = '```json\n{"action":"read","path":"index.html"}\n```'
seen = scripted([
    reads, reads, reads,
    '```json\n{"action":"write","path":"index.html","content":"tiny fixed"}\n```',
    reads,
    '```json\n{"action":"finish","summary":"ok"}\n```',
])
res = agent.run_agent("fix menu", max_steps=10, plan=False,
                      backend=StubBackend(files={"index.html": big}))
obs = observations(seen["calls"][-1])
check("first read is served (clipped)",
      "<!DOCTYPE html>" in obs[0] and "chars omitted from the middle" in obs[0])
check("clip hides the middle (the trap this guards)",
      "KEYDOWN_HANDLER_HERE" not in obs[0])
check("second read returns the unchanged note",
      "unchanged — you already read index.html at step 1" in obs[1])
check("note is short, not another 12KB clip", len(obs[1]) < 600)
check("third read notes the LATEST identical read (step 2)",
      "at step 2" in obs[2])
check("note points at grep/sed for the hidden part",
      "grep or sed" in obs[2])
check("changed content is served in full again",
      "tiny fixed" in obs[4] and "unchanged" not in obs[4])

# --- 5. seeded workspace primes bias-to-action -------------------------------
seen = scripted(['```json\n{"action":"finish","summary":"ok"}\n```'])
with tempfile.TemporaryDirectory() as seed:
    agent.run_agent("fix it", max_steps=5, plan=False, seed_dir=seed,
                    backend=StubBackend(restorable=True))
msgs = seen["calls"][0]
ws = [m["content"] for m in msgs if m["role"] == "user"
      and m["content"].startswith("WORKSPACE STATE")]
check("restored seed primes a workspace message", len(ws) == 1)
check("primed message biases toward the smallest edit",
      "smallest edit" in ws[0] and "do not spend the whole budget" in ws[0])
seen = scripted(['```json\n{"action":"finish","summary":"ok"}\n```'])
with tempfile.TemporaryDirectory() as seed:
    agent.run_agent("fix it", max_steps=5, plan=False, seed_dir=seed,
                    backend=StubBackend(restorable=False))
ws = [m["content"] for m in seen["calls"][0] if m["role"] == "user"
      and m["content"].startswith("WORKSPACE STATE")]
check("failed restore does NOT claim an existing build",
      len(ws) == 1 and "smallest edit" not in ws[0] and "FAILED" in ws[0])

# --- 6. audit start records the coder, never the key -------------------------
seen = scripted(['```json\n{"action":"finish","summary":"ok"}\n```'])
res = agent.run_agent("t", max_steps=3, plan=False, backend=StubBackend(),
                      coder={"url": "http://cdr:8080/v1/chat/completions",
                             "key": "SECRET-ABC", "model": "test-coder-1"})
start = json.loads(open(res["audit"]).readline())
check("audit start logs coder url + model",
      start.get("coder", {}).get("url") == "http://cdr:8080/v1/chat/completions"
      and start["coder"].get("model") == "test-coder-1")
check("coder key NEVER reaches the audit log",
      "SECRET-ABC" not in open(res["audit"]).read())
seen = scripted(['```json\n{"action":"finish","summary":"ok"}\n```'])
res = agent.run_agent("t", max_steps=3, plan=False, backend=StubBackend())
start = json.loads(open(res["audit"]).readline())
check("default coder logs the resolved url",
      start.get("coder", {}).get("url", "").startswith("http"))

# --- 7. exhaustion path still intact (regression) ----------------------------
seen = scripted(['```json\n{"action":"run","cmd":"true"}\n```'])
res = agent.run_agent("t", max_steps=2, plan=False, backend=StubBackend())
check("exhausted run still flagged, not done",
      res.get("exhausted") is True and res["done"] is False)

# --- 8. parse-fail retry carries the counter ---------------------------------
seen = scripted(["not an action at all",
                 '```json\n{"action":"finish","summary":"ok"}\n```'])
res = agent.run_agent("t", max_steps=6, plan=False, backend=StubBackend())
retry = [m["content"] for m in seen["calls"][-1] if m["role"] == "user"
         and "not a valid action" in m["content"]]
check("parse-fail retry names the spent step",
      len(retry) == 1 and "(step 1/6 spent)" in retry[0])

print(f"\n{len(ok)} passed, {len(bad)} failed")
raise SystemExit(1 if bad else 0)
