"""Regression for the Docker-free LOCAL workspace (opt-in, unsandboxed).

The backend runs commands directly on the host, so the tests here are REAL
(no stubs): real subprocesses in temp dirs. The gate matters as much as the
mechanics — 'local' must not exist unless CREWE_AGENT_ALLOW_LOCAL=1.
"""
import os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import router, agent
from local_workspace import LocalWorkspace

tmp = tempfile.mkdtemp()
router.USERS_FILE = os.path.join(tmp, "u.json")
router.USERDATA_ROOT = os.path.join(tmp, "d")
router.app.config["SESSION_COOKIE_SECURE"] = False

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS  " if c else "  FAIL  ") + n + (f"   [{d}]" if d and not c else ""))

print("--- LocalWorkspace mechanics (real subprocesses) ---")
base = tempfile.mkdtemp()
with LocalWorkspace("run1", workdir_base=base) as ws:
    code, out, _ = ws.run("echo hello && pwd")
    check("run executes in the per-run workdir",
          code == 0 and "hello" in out and out.strip().endswith("run1"), out)
    check("timeout -> observation exit 124, not an exception",
          ws.run("sleep 5", timeout=1)[0] == 124)
    ws.write_file("sub/app.py", "print('local-ok')\n")
    check("write + read round-trip", ws.read_file("sub/app.py") == "print('local-ok')\n")
    check("run sees written files", "local-ok" in ws.run("python3 sub/app.py")[1])
    check("missing file reads as None", ws.read_file("nope.txt") is None)
    check("file list relative + sorted", ws.list_files() == ["sub/app.py"])
    try:
        ws.write_file("../escape.txt", "x")
        escaped = True
    except ValueError:
        escaped = False
    check("write outside the workspace refused", not escaped)
    try:
        ws.read_file("../../etc/hostname")
        rescaped = True
    except ValueError:
        rescaped = False
    check("read outside the workspace refused", not rescaped)
    snap = tempfile.mkdtemp()
    os.symlink("/etc/hostname", os.path.join(ws.workdir, "sneaky"))
    ws.snapshot(snap)
    check("snapshot copies files, skips symlinks",
          os.path.isfile(os.path.join(snap, "sub/app.py"))
          and not os.path.exists(os.path.join(snap, "sneaky")))
with LocalWorkspace("run2", workdir_base=base) as ws2:
    check("restore seeds a new workspace",
          ws2.restore(snap) and ws2.read_file("sub/app.py") == "print('local-ok')\n")
    check("restore of empty dir -> False",
          ws2.restore(tempfile.mkdtemp()) is False)

print("--- gating: local target only exists when opted in ---")
router.create_user("admin@x.com", "correct-horse-battery", is_admin=True)
adm = router.app.test_client()
adm.post("/login", data={"email": "admin@x.com", "password": "correct-horse-battery"})
os.environ.pop("CREWE_AGENT_ALLOW_LOCAL", None)
names = [t["name"] for t in adm.get("/agent/hosts").get_json()["targets"]]
check("local ABSENT from targets by default", "local" not in names, str(names))
r = adm.post("/agent/run", json={"task": "x", "target": "local"})
check("run with local -> 400 when disabled", r.status_code == 400, str(r.get_json()))
check("run slot not leaked", router.AGENT_ACTIVE.acquire(blocking=False)
      and (router.AGENT_ACTIVE.release() or True))

os.environ["CREWE_AGENT_ALLOW_LOCAL"] = "1"
try:
    targets = adm.get("/agent/hosts").get_json()["targets"]
    loc = next((t for t in targets if t["name"] == "local"), None)
    check("local appears once enabled, kind=local",
          loc is not None and loc.get("kind") == "local", str(targets))

    print("--- a real run on the local target ---")
    os.environ["CREWE_AGENT_LOCAL_DIR"] = tempfile.mkdtemp()
    seen = {}
    def fake_run(task, **kw):
        seen.update(kw)
        with kw["backend"] as sb:
            sb.write_file("out.txt", "built-locally")
            code, out, _ = sb.run("cat out.txt")
        os.makedirs(kw["snapshot_dir"], exist_ok=True)
        with open(os.path.join(kw["snapshot_dir"], "out.txt"), "w") as f:
            f.write(out)
        return {"done": True, "summary": out, "files": ["out.txt"], "steps": 1}
    agent.run_agent = fake_run
    j = adm.post("/agent/run", json={"task": "build", "target": "local"}).get_json()
    for _ in range(50):
        p = adm.get(f"/agent/progress/{j['job_id']}?since=0").get_json()
        if p.get("done"):
            break
        time.sleep(0.1)
    check("run completes on local backend",
          p.get("done") and p.get("summary") == "built-locally", str(p.get("summary")))
    check("backend was a LocalWorkspace",
          type(seen.get("backend")).__name__ == "LocalWorkspace",
          type(seen.get("backend")).__name__)
finally:
    os.environ.pop("CREWE_AGENT_ALLOW_LOCAL", None)
    os.environ.pop("CREWE_AGENT_LOCAL_DIR", None)

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
