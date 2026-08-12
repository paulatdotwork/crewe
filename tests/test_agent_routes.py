import os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import router, agent

tmp=tempfile.mkdtemp()
router.USERS_FILE=os.path.join(tmp,"u.json"); router.USERDATA_ROOT=os.path.join(tmp,"d")
router.app.config["SESSION_COOKIE_SECURE"]=False

# fake agent run: emits events, writes a file into the snapshot dir, returns
def fake_run(task, *, max_steps=15, network="egress", on_event=None, snapshot_dir=None, should_cancel=None, backend=None, plan=True, plan_text=None, mode="code", model_url=None, seed_dir=None, coder=None):
    on_event and on_event({"t":"action","step":1,"action":"write","detail":{"path":"a.py"}})
    on_event and on_event({"t":"observation","step":1,"obs":"wrote a.py"})
    os.makedirs(snapshot_dir, exist_ok=True)
    with open(os.path.join(snapshot_dir,"a.py"),"w") as f: f.write("print('hello from agent')\n")
    return {"done":True,"summary":"built a.py","files":["a.py"],"steps":1}
agent.run_agent = fake_run

P,F=[],[]
def check(n,c,d=""):
    (P if c else F).append(n); print(("  PASS  " if c else "  FAIL  ")+n+(f"   [{d}]" if d and not c else ""))

router.create_user("admin@x.com","correct-horse-battery",is_admin=True)
router.create_user("guest@x.com","correct-horse-battery",is_admin=False)
adm=router.app.test_client(); adm.post("/login",data={"email":"admin@x.com","password":"correct-horse-battery"})
gst=router.app.test_client(); gst.post("/login",data={"email":"guest@x.com","password":"correct-horse-battery"})

print("--- admin gating ---")
check("admin reaches /agent", adm.get("/agent").status_code==200)
check("non-admin BLOCKED /agent (403)", gst.get("/agent",headers={"Accept":"application/json"}).status_code==403)
check("non-admin BLOCKED /agent/run (403)", gst.post("/agent/run",json={"task":"x"}).status_code==403)

print("--- run flow (mocked agent) ---")
r=adm.post("/agent/run",json={"task":"build a.py"}); j=r.get_json()
check("run starts -> job_id", r.status_code==200 and j.get("job_id"), str(j))
jid=j["job_id"]
# poll to completion
done=None
for _ in range(50):
    p=adm.get(f"/agent/progress/{jid}?since=0").get_json()
    if p.get("done"): done=p; break
    time.sleep(0.1)
check("run completes", bool(done))
check("transcript has events", done and len(done.get("events",[]))>=2, str(done and len(done.get('events',[]))))
check("summary present", done and done.get("summary")=="built a.py")
check("files listed", done and done.get("files")==["a.py"], str(done and done.get("files")))

print("--- file access + isolation ---")
check("files endpoint lists a.py", adm.get(f"/agent/files/{jid}").get_json().get("files")==["a.py"])
fc=adm.get(f"/agent/file/{jid}/a.py")
check("file content served", fc.status_code==200 and b"hello from agent" in fc.data)
check("traversal blocked", adm.get(f"/agent/file/{jid}/../../../../etc/passwd").status_code in (400,404))
z=adm.get(f"/agent/download/{jid}.zip")
check("zip download works", z.status_code==200 and z.data[:2]==b"PK")
check("non-admin cannot read admin's run file", gst.get(f"/agent/file/{jid}/a.py").status_code==403)
# owned_job: guest can't even see the job progress
check("non-admin cannot poll admin's job", gst.get(f"/agent/progress/{jid}").status_code==403)

print("--- one-at-a-time guard ---")
router.AGENT_ACTIVE.acquire()   # simulate a run in progress
try:
    check("second run -> 429 while active", adm.post("/agent/run",json={"task":"y"}).status_code==429)
finally:
    router.AGENT_ACTIVE.release()

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
