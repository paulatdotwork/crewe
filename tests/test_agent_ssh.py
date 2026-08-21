import os, sys, tempfile, json, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import router, agent

tmp=tempfile.mkdtemp()
router.USERS_FILE=os.path.join(tmp,"u.json"); router.USERDATA_ROOT=os.path.join(tmp,"d")
router.app.config["SESSION_COOKIE_SECURE"]=False
# allowlist with one remote host
router.AGENT_HOSTS_FILE=os.path.join(tmp,"hosts.json")
json.dump({"hosts":[{"name":"buildbox","host":"10.0.0.5","user":"pal","workdir":"~/crewe-agent"}]},
          open(router.AGENT_HOSTS_FILE,"w"))

captured={}
def fake_run(task, *, max_steps=15, network="egress", on_event=None, snapshot_dir=None, should_cancel=None, backend=None, plan=True, plan_text=None, mode="code", model_url=None, seed_dir=None, coder=None, **kw):
    captured["backend"]=backend
    on_event and on_event({"t":"action","step":1,"action":"run","detail":{"cmd":"echo hi"}})
    os.makedirs(snapshot_dir, exist_ok=True)
    open(os.path.join(snapshot_dir,"out.txt"),"w").write("ok")
    return {"done":True,"summary":"ok","files":["out.txt"],"steps":1}
agent.run_agent=fake_run

P,F=[],[]
def check(n,c,d=""):
    (P if c else F).append(n); print(("  PASS  " if c else "  FAIL  ")+n+(f"   [{d}]" if d and not c else ""))

router.create_user("admin@x.com","correct-horse-battery",is_admin=True)
router.create_user("guest@x.com","correct-horse-battery",is_admin=False)
adm=router.app.test_client(); adm.post("/login",data={"email":"admin@x.com","password":"correct-horse-battery"})
gst=router.app.test_client(); gst.post("/login",data={"email":"guest@x.com","password":"correct-horse-battery"})

def wait_done(jid):
    for _ in range(60):
        p=adm.get(f"/agent/progress/{jid}?since=0").get_json()
        if p.get("done"): return p
        time.sleep(0.1)
    return None

print("--- /agent/hosts ---")
h=adm.get("/agent/hosts").get_json()
names=[t["name"] for t in h["targets"]]
# Assert the CONTRACT, not a snapshot: built-in targets first, then whatever
# hosts THIS TEST's fixture allowlist defines. The old hardcoded list broke
# twice — once when a second SSH host was added, once when blender was removed.
_expected = ["sandbox"] + [h["name"] for h in
                           json.load(open(router.AGENT_HOSTS_FILE))["hosts"]]
check("lists the built-in targets plus every allowlisted host",
      names == _expected, f"{names} != {_expected}")
check("NO host/user/key leaked to client", "10.0.0.5" not in json.dumps(h) and "pal" not in [t.get('user') for t in h['targets']])
check("non-admin blocked from /agent/hosts", gst.get("/agent/hosts",headers={"Accept":"application/json"}).status_code==403)

print("--- target routing (allowlist = boundary) ---")
r=adm.post("/agent/run",json={"task":"t","target":"buildbox"}); check("run target=buildbox accepted", r.status_code==200)
p=wait_done(r.get_json()["job_id"]); check("buildbox run completed", bool(p))
import ssh_workspace
bk=captured.get("backend")
check("backend is an SSHWorkspace", isinstance(bk, ssh_workspace.SSHWorkspace))
check("backend resolved to the allowlisted host", bk and bk.host=="10.0.0.5" and bk.user=="pal", f"{bk and bk.host}")

captured.clear()
r=adm.post("/agent/run",json={"task":"t","target":"sandbox"}); wait_done(r.get_json()["job_id"])
check("target=sandbox -> backend None (Docker)", captured.get("backend") is None)

print("--- allowlist rejects arbitrary hosts ---")
r=adm.post("/agent/run",json={"task":"t","target":"evil.attacker.com"})
check("unknown/arbitrary target -> 400", r.status_code==400, str(r.status_code))
r=adm.post("/agent/run",json={"task":"t","target":"10.0.0.5"})
check("raw IP not on allowlist -> 400", r.status_code==400)

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
