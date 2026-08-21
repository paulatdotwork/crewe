"""Regression for the agent run preview + test bench + follow-up surface.

/agent/preview/<run>/ serves the snapshot as a static site (true mimetypes,
CSP-sandboxed into an opaque origin — the pages are model-written and must
never reach the admin session). /agent/test/<run> runs one command in a fresh
sandbox seeded from the snapshot, holding the AGENT_ACTIVE slot so the
container reaper's one-at-a-time argument still holds.
"""
import os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import router, agent, sandbox
router.AGENT_BROWSER_CHECK = False   # no headless browser in unit tests

tmp = tempfile.mkdtemp()
router.USERS_FILE = os.path.join(tmp, "u.json")
router.USERDATA_ROOT = os.path.join(tmp, "d")
router.app.config["SESSION_COOKIE_SECURE"] = False

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS  " if c else "  FAIL  ") + n + (f"   [{d}]" if d and not c else ""))

FILES = {"index.html": "<html><body>MARKER-INDEX<link rel=stylesheet "
                       "href='style.css'></body></html>",
         "style.css": "body{color:red}",
         "sub/page.html": "<html>MARKER-SUB</html>"}
def fake_run(task, **kw):
    os.makedirs(os.path.join(kw["snapshot_dir"], "sub"), exist_ok=True)
    for p, c in FILES.items():
        with open(os.path.join(kw["snapshot_dir"], p), "w") as f:
            f.write(c)
    return {"done": True, "summary": "built", "files": sorted(FILES), "steps": 1}
agent.run_agent = fake_run

router.create_user("admin@x.com", "correct-horse-battery", is_admin=True)
router.create_user("guest@x.com", "correct-horse-battery", is_admin=False)
adm = router.app.test_client(); adm.post("/login", data={"email": "admin@x.com", "password": "correct-horse-battery"})
gst = router.app.test_client(); gst.post("/login", data={"email": "guest@x.com", "password": "correct-horse-battery"})

def run_job():
    j = adm.post("/agent/run", json={"task": "build"}).get_json()
    jid = j["job_id"]
    for _ in range(50):
        if adm.get(f"/agent/progress/{jid}?since=0").get_json().get("done"):
            return jid
        time.sleep(0.1)
    return jid

print("--- preview: static site from the snapshot ---")
jid = run_job()
r = adm.get(f"/agent/preview/{jid}/")
check("root serves index.html", r.status_code == 200 and b"MARKER-INDEX" in r.data, str(r.status_code))
check("html gets a real html mimetype", "text/html" in r.content_type, r.content_type)
check("CSP sandbox on every response",
      r.headers.get("Content-Security-Policy") == "sandbox allow-scripts",
      str(r.headers.get("Content-Security-Policy")))
r = adm.get(f"/agent/preview/{jid}/style.css")
check("css gets text/css (relative links work)",
      r.status_code == 200 and "text/css" in r.content_type, r.content_type)
check("css is CSP-sandboxed too",
      r.headers.get("Content-Security-Policy") == "sandbox allow-scripts")
r = adm.get(f"/agent/preview/{jid}/sub/page.html")
check("nested paths served", r.status_code == 200 and b"MARKER-SUB" in r.data)
r = adm.get(f"/agent/preview/{jid}/no-such-file.js")
check("missing file -> 404", r.status_code == 404)
r = adm.get(f"/agent/preview/{jid}/%2e%2e/%2e%2e/%2e%2e/etc/passwd")
check("traversal blocked", r.status_code != 200 or b"root:" not in r.data, str(r.status_code))
check("bad run id -> 400", adm.get("/agent/preview/..%2f..%2fx/").status_code in (400, 404))
check("non-admin blocked",
      gst.get(f"/agent/preview/{jid}/", headers={"Accept": "application/json"}).status_code == 403)

print("--- preview: no-html fallback ---")
def fake_run_cli(task, **kw):
    os.makedirs(kw["snapshot_dir"], exist_ok=True)
    with open(os.path.join(kw["snapshot_dir"], "cli.py"), "w") as f:
        f.write("print('hi')\n")
    return {"done": True, "summary": "cli", "files": ["cli.py"], "steps": 1}
agent.run_agent = fake_run_cli
jid2 = run_job()
r = adm.get(f"/agent/preview/{jid2}/")
check("no html -> plain explanation, not an error",
      r.status_code == 200 and b"no HTML" in r.data, r.data[:80])

print("--- zip download: symlinks never packaged ---")
UID0 = next(u["id"] for u in __import__("json").load(open(router.USERS_FILE))["users"]
            if u["email"] == "admin@x.com")
snapdir = os.path.join(router.agent_runs_dir(UID0), jid)
os.symlink("/etc/hostname", os.path.join(snapdir, "sneaky-link"))
import io, zipfile
z = zipfile.ZipFile(io.BytesIO(adm.get(f"/agent/download/{jid}.zip").data))
check("symlink excluded from the zip (host-file exfil guard)",
      "sneaky-link" not in z.namelist(), str(z.namelist()))
check("real files still zipped", "index.html" in z.namelist())
os.unlink(os.path.join(snapdir, "sneaky-link"))

print("--- entry-point detection (one-click run for CLI builds) ---")
E = router._entry_cmd
check("single script -> python3 it", E(["cli.py"]) == "python3 cli.py", E(["cli.py"]))
check("conventional name wins over others",
      E(["util.py", "main.py", "data.json"]) == "python3 main.py")
check("top-level entry beats a nested one",
      E(["repo/main.py", "main.py"]) == "python3 main.py")
check("nested-only entry still found",
      E(["repo/app.py", "repo/util.py"]) == "python3 repo/app.py")
check("requirements.txt prefixes an install",
      E(["main.py", "requirements.txt"])
      == "pip install -r requirements.txt && python3 main.py")
check("many scripts, no conventional name -> no guess",
      E(["a.py", "b.py"]) is None)
check("html-only build -> no command", E(["index.html", "style.css"]) is None)
j = adm.get(f"/agent/progress/{jid2}?since=0").get_json()
check("progress payload carries entry_cmd",
      j.get("entry_cmd") == "python3 cli.py", str(j.get("entry_cmd")))
j = adm.get(f"/agent/progress/{jid}?since=0").get_json()
check("html build has no entry_cmd", j.get("entry_cmd") is None, str(j.get("entry_cmd")))

print("--- test bench (stubbed sandbox) ---")
class FakeSB:
    seeded = None
    def __init__(self, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def restore(self, src): FakeSB.seeded = src; return True
    def run(self, cmd, timeout=None):
        FakeSB.cmd, FakeSB.timeout = cmd, timeout
        return 0, "hello-from-test\n", ""
sandbox.Sandbox = FakeSB
router._reap_sandbox_containers = lambda: None
r = adm.post(f"/agent/test/{jid2}", json={"cmd": "python3 cli.py"})
j = r.get_json()
check("command runs, output returned",
      r.status_code == 200 and j.get("exit_code") == 0
      and "hello-from-test" in j.get("stdout", ""), str(j))
check("workspace seeded from THIS run's snapshot",
      FakeSB.seeded and FakeSB.seeded.endswith(jid2), str(FakeSB.seeded))
check("agent cmd timeout applied", FakeSB.timeout == agent.CMD_TIMEOUT, str(FakeSB.timeout))
check("empty command -> 400", adm.post(f"/agent/test/{jid2}", json={}).status_code == 400)
check("bad run id -> 400", adm.post("/agent/test/nope!!", json={"cmd": "x"}).status_code == 400)
check("unknown run -> 404",
      adm.post("/agent/test/deadbeef00", json={"cmd": "x"}).status_code == 404)
check("non-admin blocked", gst.post(f"/agent/test/{jid2}", json={"cmd": "x"},
      headers={"Accept": "application/json"}).status_code == 403)
router.AGENT_ACTIVE.acquire()
try:
    check("agent busy -> 429",
          adm.post(f"/agent/test/{jid2}", json={"cmd": "x"}).status_code == 429)
finally:
    router.AGENT_ACTIVE.release()
check("slot released after a test",
      router.AGENT_ACTIVE.acquire(blocking=False) and (router.AGENT_ACTIVE.release() or True))
class BadSB(FakeSB):
    def restore(self, src): return False
sandbox.Sandbox = BadSB
r = adm.post(f"/agent/test/{jid2}", json={"cmd": "x"})
check("failed seed -> 500 with error", r.status_code == 500, str(r.get_json()))
check("slot released after a FAILED test",
      router.AGENT_ACTIVE.acquire(blocking=False) and (router.AGENT_ACTIVE.release() or True))

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
