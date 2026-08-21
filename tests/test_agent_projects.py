"""Regression for agent PROJECTS — explicit continuity.

The bug this kills: continuity used to be implicit (agent_last.json), so every
link-less task seeded from whatever ran last — a brand-new request woke up
inside the previous build's files (the old index.html greeted the new run).
Now every run belongs to a project; a workspace is seeded ONLY when the admin
explicitly continues a project, and projects can be browsed and deleted.
"""
import json, os, sys, tempfile, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import router, agent
router.AGENT_BROWSER_CHECK = False   # no headless browser in unit tests

tmp = tempfile.mkdtemp()
router.USERS_FILE = os.path.join(tmp, "u.json")
router.USERDATA_ROOT = os.path.join(tmp, "d")
router.app.config["SESSION_COOKIE_SECURE"] = False

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS  " if c else "  FAIL  ") + n + (f"   [{d}]" if d and not c else ""))

CALLS = []
def fake_run(task, **kw):
    CALLS.append({"task": task, "seed_dir": kw.get("seed_dir")})
    os.makedirs(kw["snapshot_dir"], exist_ok=True)
    with open(os.path.join(kw["snapshot_dir"], "index.html"), "w") as f:
        f.write("<html>old build</html>")
    return {"done": True, "summary": "built the thing", "files": ["index.html"],
            "steps": 1}
agent.run_agent = fake_run

router.create_user("admin@x.com", "correct-horse-battery", is_admin=True)
adm = router.app.test_client()
adm.post("/login", data={"email": "admin@x.com", "password": "correct-horse-battery"})

def run(payload):
    j = adm.post("/agent/run", json=payload).get_json()
    assert "job_id" in j, j
    for _ in range(50):
        if adm.get(f"/agent/progress/{j['job_id']}?since=0").get_json().get("done"):
            break
        time.sleep(0.1)
    return j

print("--- every run gets a project; fresh runs NEVER seed ---")
j1 = run({"task": "build a snake game please"})
check("run response names its project", bool(j1.get("project")), str(j1))
check("first run starts clean", CALLS[0]["seed_dir"] is None, str(CALLS[0]["seed_dir"]))
j2 = run({"task": "a completely different recipe CLI"})
check("a NEW request does not inherit the old build (THE bug)",
      CALLS[1]["seed_dir"] is None, str(CALLS[1]["seed_dir"]))
check("it got its own project", j2["project"] != j1["project"])

print("--- continuing a project seeds its workspace + summary ---")
j3 = run({"task": "make the snake faster", "project": j1["project"]})
check("continued run seeds from the project's snapshot",
      CALLS[2]["seed_dir"] and CALLS[2]["seed_dir"].endswith(j1["job_id"]),
      str(CALLS[2]["seed_dir"]))
check("previous summary rides along as context",
      "built the thing" in CALLS[2]["task"], CALLS[2]["task"][-120:])
check("same project id", j3["project"] == j1["project"])
j4 = run({"task": "review https://github.com/x/y please", "project": j1["project"]})
check("repo-link task starts fresh even inside a project",
      CALLS[3]["seed_dir"] is None, str(CALLS[3]["seed_dir"]))

print("--- browsing the shelf ---")
ps = adm.get("/agent/projects").get_json()["projects"]
check("both projects listed", len(ps) == 2, str(len(ps)))
p1 = next(p for p in ps if p["id"] == j1["project"])
check("newest-updated first", ps[0]["id"] == j1["project"])
check("title from the first request", p1["title"] == "build a snake game please")
check("run count accumulates", p1["runs"] == 3, str(p1["runs"]))
check("summary shown", p1["summary"] == "built the thing")
check("html flagged for preview", p1["has_html"] is True and p1["job_id"] == j4["job_id"],
      str((p1["has_html"], p1["job_id"])))
check("listing carries entry_cmd (none for an html-only build)",
      "entry_cmd" in p1 and p1["entry_cmd"] is None, str(p1.get("entry_cmd")))
check("unknown project on run -> 400",
      adm.post("/agent/run", json={"task": "x", "project": "nope"}).status_code == 400)
check("run slot not leaked by the 400",
      router.AGENT_ACTIVE.acquire(blocking=False) and (router.AGENT_ACTIVE.release() or True))

print("--- deleting a project removes its files ---")
UID = next(u["id"] for u in json.load(open(router.USERS_FILE))["users"]
           if u["email"] == "admin@x.com")
snapdirs = [os.path.join(router.agent_runs_dir(UID), j["job_id"])
            for j in (j1, j3, j4)]
check("snapshots exist before delete", all(os.path.isdir(d) for d in snapdirs))
r = adm.post(f"/agent/projects/{j1['project']}/delete")
check("delete ok", r.status_code == 200, str(r.get_json()))
check("gone from the shelf", all(p["id"] != j1["project"] for p in
      adm.get("/agent/projects").get_json()["projects"]))
check("every run's files removed", not any(os.path.isdir(d) for d in snapdirs))
check("other project untouched", os.path.isdir(os.path.join(
      router.agent_runs_dir(UID), j2["job_id"])))
check("unknown project delete -> 404",
      adm.post("/agent/projects/nope/delete").status_code == 404)

print("--- a project with a live run refuses deletion ---")
gate = threading.Event()
def slow_run(task, **kw):
    gate.wait(10)
    os.makedirs(kw["snapshot_dir"], exist_ok=True)
    return {"done": True, "summary": "s", "files": [], "steps": 1}
agent.run_agent = slow_run
j5 = adm.post("/agent/run", json={"task": "slow build"}).get_json()
time.sleep(0.2)
r = adm.post(f"/agent/projects/{j5['project']}/delete")
check("delete during its run -> 409", r.status_code == 409, str(r.get_json()))
gate.set()
for _ in range(50):
    if adm.get(f"/agent/progress/{j5['job_id']}?since=0").get_json().get("done"):
        break
    time.sleep(0.1)
check("deletable once the run is done",
      adm.post(f"/agent/projects/{j5['project']}/delete").status_code == 200)

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
