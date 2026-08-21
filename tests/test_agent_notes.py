"""Mid-run notes: the user steers a running agent (offline, deterministic).

Why: the only control during a run was Stop. Every current coding harness
lets the user talk to the agent while it works; here a note is queued via
POST /agent/note/<job>, drained by the loop after the action in flight, and
delivered with that observation (outside the clip). A note pending when the
model calls finish defers the finish by one step. Human steering only — no
critic, no rounds — and bounded: 2000 chars, 20 pending.

No model, no Docker: the coder is scripted, the workspace is a stub, the
router's run is a blocking fake.
"""
import os
import sys
import tempfile
import threading
import time

_AUDIT_TMP = tempfile.mkdtemp(prefix="crewe-audit-test-")
os.environ["CREWE_AGENT_AUDIT_DIR"] = _AUDIT_TMP
os.environ.pop("CREWE_AGENT_URL", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)
import agent
import router

ok, bad = [], []


def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


class StubBackend:
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.ran = []

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


class BigBackend(StubBackend):
    def run(self, cmd, timeout=None):
        self.ran.append(cmd)
        return 0, ("x" * 90 + "\n") * 300, ""


def scripted(replies):
    seen = {"calls": [], "n": 0}

    def fake_chat(messages, **kw):
        seen["calls"].append([dict(m) for m in messages])
        seen["n"] += 1
        return replies[min(seen["n"] - 1, len(replies) - 1)]

    agent._chat = fake_chat
    return seen


def run(replies, backend=None, max_steps=8, **kw):
    seen = scripted(replies)
    events = []
    res = agent.run_agent("do the task", max_steps=max_steps, plan=False,
                          backend=backend or StubBackend(),
                          on_event=events.append, **kw)
    return res, seen, events


def queue_puller(initial):
    q = list(initial)

    def pull():
        n, q[:] = list(q), []
        return n
    pull.q = q
    return pull


RUN = '```json\n{"action":"run","cmd":"ls"}\n```'
RUN2 = '```json\n{"action":"run","cmd":"pwd"}\n```'
FINISH = '```json\n{"action":"finish","summary":"done"}\n```'

# ---- delivery with the next observation ---------------------------------------------
print("--- delivery ---")
pull = queue_puller(["use tabs, not spaces"])
res, seen, events = run([RUN, RUN2, FINISH], pull_notes=pull)
obs1 = seen["calls"][1][-1]["content"]
check("a note queued before step 1 rides on step 1's observation",
      obs1.startswith("OBSERVATION (step 1/8)") and "NOTE FROM THE USER" in obs1
      and "use tabs, not spaces" in obs1, obs1[-200:])
check("a 'note' event marks the delivery step",
      [e for e in events if e.get("t") == "note"] == [{"t": "note", "step": 1, "text": "use tabs, not spaces"}],
      str([e for e in events if e.get("t") == "note"]))
final = seen["calls"][-1]
check("delivered once — it is not repeated on later observations",
      sum(m["content"].count("use tabs, not spaces") for m in final) == 1)
check("the queue was drained", pull.q == [])
check("run finished normally", res["done"] and res["steps"] == 3)

pull = queue_puller(["check the tail"])
res, seen, events = run([RUN, RUN2, FINISH], backend=BigBackend(), pull_notes=pull)
obs1 = seen["calls"][1][-1]["content"]
check("the note survives a clipped observation (it rides outside the clip)",
      "chars omitted from the middle" in obs1 and obs1.rstrip().endswith("check the tail"), obs1[-120:])

pull = queue_puller(["a1", "b2", "c3"])
res, seen, events = run([RUN, FINISH], pull_notes=pull)
obs1 = seen["calls"][1][-1]["content"]
check("several pending notes are delivered together, in order",
      obs1.index("a1") < obs1.index("b2") < obs1.index("c3") and obs1.count("\n---\n") == 2, obs1[-80:])
check("one event per note", len([e for e in events if e.get("t") == "note"]) == 3)

pull = queue_puller([f"note-{i}" for i in range(20)])
res, seen, events = run([RUN, FINISH], pull_notes=pull)
obs1 = seen["calls"][1][-1]["content"]
check("everything pulled is delivered — 20 pending (the router's cap) arrive intact",
      all(f"note-{i}" in obs1 for i in range(20))
      and len([e for e in events if e.get("t") == "note"]) == 20, obs1[-120:])

# ---- a pending note defers finish ----------------------------------------------------
print("--- finish deferral ---")
calls = {"n": 0}
def late_pull():
    calls["n"] += 1
    return ["also add tests"] if calls["n"] == 2 else []
res, seen, events = run([RUN, FINISH, FINISH], pull_notes=late_pull)
check("finish is deferred once when a note arrived, then accepted",
      res["done"] and res["steps"] == 3, str(res.get("steps")))
deferral = seen["calls"][2][-1]["content"]
check("the deferral observation carries the note and says why",
      deferral.startswith("OBSERVATION (step 2/8):\nNOT FINISHED") and "also add tests" in deferral, deferral[:160])
check("deferral is visible in the transcript",
      any(e.get("t") == "warn" and "finish deferred" in e.get("note", "") for e in events))
check("no finish-gate bounce was consumed by the note (gate never fired)",
      not any("finish refused" in e.get("note", "") for e in events))

always = lambda: ["late"]
res, seen, events = run([RUN, FINISH], max_steps=2, pull_notes=always)
check("with no step left the finish stands; a note is delivered at step 1 but not drained at the final step",
      res["done"] and res["steps"] == 2
      and [e["step"] for e in events if e.get("t") == "note"] == [1], str([e for e in events if e.get("t") == "note"]))
check("...and the note that could not be delivered is reported, not dropped",
      res.get("undelivered_notes") == 1
      and any(e.get("t") == "warn" and "too late" in e.get("note", "") and "late" in e.get("note", "")
              for e in events), str([e for e in events if e.get("t") == "warn"]))

calls = {"n": 0}
def second_call():
    calls["n"] += 1
    return ["also add tests"] if calls["n"] == 2 else []
res, seen, events = run([RUN, FINISH, FINISH], max_steps=3, pull_notes=second_call)
check("a note at a finish with exactly ONE step left does not defer (that would exhaust the run)",
      res["done"] and res["steps"] == 2 and not res.get("exhausted")
      and res.get("undelivered_notes") == 1, str({k: res.get(k) for k in ("done", "steps", "exhausted", "undelivered_notes")}))

calls = {"n": 0}
def fifth_call():
    calls["n"] += 1
    return ["tighten it"] if calls["n"] == 5 else []
res, seen, events = run([RUN] * 4 + [FINISH, FINISH], max_steps=8, pull_notes=fifth_call)
deferral = seen["calls"][5][-1]["content"]
check("a deferral inside the last 3 steps carries the countdown",
      "NOT FINISHED" in deferral and "[3 action(s) left — commit now" in deferral, deferral[-160:])

# ---- bounds and robustness --------------------------------------------------------------
print("--- bounds ---")
def boom():
    raise RuntimeError("queue on fire")
res, seen, events = run([RUN, FINISH], pull_notes=boom)
check("a failing queue is ignored", res["done"] and not any("NOTE FROM" in m["content"] for m in seen["calls"][-1]))
pull = queue_puller(["", "   ", None])
res, seen, events = run([RUN, FINISH], pull_notes=pull)
check("blank notes are dropped", not any("NOTE FROM" in m["content"] for m in seen["calls"][-1]))
pull = queue_puller(["N" * 3000 + "ZZZ"])
res, seen, events = run([RUN, FINISH], pull_notes=pull)
obs1 = seen["calls"][1][-1]["content"]
check("a note is clipped to NOTE_MAX_CHARS", "N" * 2000 in obs1 and "ZZZ" not in obs1)
res, seen, events = run([RUN, FINISH])
check("no queue -> no note plumbing in the messages",
      not any("NOTE FROM" in m["content"] for m in seen["calls"][-1]))

# ---- router: POST /agent/note ---------------------------------------------------------------
print("--- router /agent/note ---")
tmp = tempfile.mkdtemp()
router.USERS_FILE = os.path.join(tmp, "u.json")
router.USERDATA_ROOT = os.path.join(tmp, "d")
router.app.config["SESSION_COOKIE_SECURE"] = False
router.AGENT_BROWSER_CHECK = False
router.create_user("admin@x.com", "correct-horse-battery", is_admin=True)
router.create_user("other@x.com", "correct-horse-battery", is_admin=True)
adm = router.app.test_client()
adm.post("/login", data={"email": "admin@x.com", "password": "correct-horse-battery"})
other = router.app.test_client()
other.post("/login", data={"email": "other@x.com", "password": "correct-horse-battery"})

GATE = threading.Event()
SEEN = {}
def blocking_run(task, **kw):
    SEEN.update(kw)
    GATE.wait(20)
    return {"done": True, "summary": "ok", "files": [], "steps": 1}
agent.run_agent = blocking_run

r = adm.post("/agent/run", json={"task": "build it"})
check("run starts", r.status_code == 200, str(r.get_json()))
job = (r.get_json() or {}).get("job_id")
for _ in range(50):
    if SEEN:
        break
    time.sleep(0.1)
check("the worker hands run_agent a pull_notes callable", callable(SEEN.get("pull_notes")))

r = adm.post(f"/agent/note/{job}", json={"text": "  use sqlite, not postgres  "})
check("a note is accepted and counted", r.status_code == 200 and r.get_json() == {"ok": True, "pending": 1}, str(r.get_json()))
j = router.AGENT_JOBS[job]
check("queued on the job, trimmed", j.get("notes") == ["use sqlite, not postgres"], str(j.get("notes")))
check("queueing is visible in the transcript at once",
      any(e.get("t") == "note_queued" and e.get("text") == "use sqlite, not postgres" and e.get("pending") == 1
          for e in j["events"]), str(j["events"][-1:]))
got = SEEN["pull_notes"]()
check("the worker's puller drains the queue", got == ["use sqlite, not postgres"] and j.get("notes") == [])
check("draining again yields nothing", SEEN["pull_notes"]() == [])

r = adm.post(f"/agent/note/{job}", json={"text": "   "})
check("an empty note -> 400", r.status_code == 400)
r = adm.post(f"/agent/note/{job}", data="[1, 2]", content_type="application/json")
check("a non-object JSON body -> 400, not a 500", r.status_code == 400, str(r.status_code))
r = adm.post(f"/agent/note/{job}", json={"text": "L" * 5000})
check("a long note is clipped server-side to 2000",
      r.status_code == 200 and len(j["notes"][-1]) == 2000 and len(j["events"][-1]["text"]) == 300)
for i in range(19):
    adm.post(f"/agent/note/{job}", json={"text": f"n{i}"})
check("20 pending is the cap", len(j["notes"]) == 20)
r = adm.post(f"/agent/note/{job}", json={"text": "one too many"})
check("the 21st waits -> 429", r.status_code == 429 and len(j["notes"]) == 20, str(r.get_json()))
r = other.post(f"/agent/note/{job}", json={"text": "mine now"})
check("another user's note -> 404 (the job is invisible to them)", r.status_code == 404)
r = adm.post("/agent/note/nope", json={"text": "x"})
check("unknown job -> 404", r.status_code == 404)
check("nothing foreign leaked into the queue", all(n != "mine now" for n in j["notes"]))

j["cancel"] = True
r = adm.post(f"/agent/note/{job}", json={"text": "after stop"})
check("a run that is stopping refuses notes -> 409 (it would never be delivered)",
      r.status_code == 409 and "after stop" not in j["notes"], str(r.get_json()))
j["cancel"] = False
GATE.set()
for _ in range(100):
    if j.get("done"):
        break
    time.sleep(0.1)
r = adm.post(f"/agent/note/{job}", json={"text": "too late"})
check("a finished run refuses notes -> 409", j.get("done") and r.status_code == 409, str(r.get_json()))
check("the run slot is free again",
      router.AGENT_ACTIVE.acquire(blocking=False) and (router.AGENT_ACTIVE.release() or True))

page = adm.get("/agent").get_data(as_text=True)
check("the page has the note bar, the sender and both event renderers",
      'id="noteBar"' in page and "function sendNote" in page
      and "note_queued" in page and "e.t==='note'" in page)
check("the note bar shows only while a run is live",
      "$('noteBar').classList.toggle('hidden',!on)" in page)

print(f"\n{len(ok)} passed, {len(bad)} failed")
if bad:
    print("FAILED:", bad)
sys.exit(1 if bad else 0)
