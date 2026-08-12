"""Regression for the agent coder-selection + timeout plumbing.

Covers the two failure modes that motivated the change:
  1. the coder was UNSETTABLE — AGENT_URL was read once at import time and no
     UI/config path existed; now /agent exposes a per-run coder chosen from the
     router's openai-kind backends, resolved SERVER-SIDE by id (same posture as
     the SSH host allowlist), and CREWE_AGENT_URL is re-read per call.
  2. runs died at step 1 with "read timeout=360" — the read timeout is now
     CREWE_AGENT_READ_TIMEOUT (default 1800), cmd budget CREWE_AGENT_CMD_TIMEOUT
     (default 600).

Uses a real local HTTP stub for the model server — no network, no live models.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import router, agent

tmp = tempfile.mkdtemp()
router.USERS_FILE = os.path.join(tmp, "u.json")
router.USERDATA_ROOT = os.path.join(tmp, "d")
router.app.config["SESSION_COOKIE_SECURE"] = False

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS  " if c else "  FAIL  ") + n + (f"   [{d}]" if d and not c else ""))

# ---- stub OpenAI-compatible model server -------------------------------------
# /v1/...      -> plain JSON reply (a backend that ignores stream:true)
# /slow/...    -> sleeps 3s before replying (read-timeout test)
# /sse/...     -> proper SSE stream, chunks "st","re","am" then [DONE]
# /drip/...    -> SSE, one chunk per 0.5s for 10s (cancel-mid-stream test)
LAST = {}
class Stub(BaseHTTPRequestHandler):
    def _sse(self, pieces, delay=0.0):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for p in pieces:
            if delay:
                time.sleep(delay)
            self.wfile.write(("data: " + json.dumps(
                {"choices": [{"delta": {"content": p}}]}) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        LAST.update(path=self.path, auth=self.headers.get("Authorization"),
                    body=json.loads(body or b"{}"))
        if self.path.startswith("/sse"):
            return self._sse(["st", "re", "am"])
        if self.path.startswith("/drip"):
            try:
                return self._sse(["x"] * 20, delay=0.5)
            except BrokenPipeError:   # client hung up after cancel — expected
                return
        if self.path.startswith("/slow"):
            time.sleep(3)
        payload = json.dumps({"choices": [{"message": {"content": "pong"}}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode())
    def log_message(self, *a):
        pass

srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

# ---- _agent_coder: server-side resolution ------------------------------------
print("--- _agent_coder resolution ---")
router.ROUTER_CONFIG = {"backends": [
    {"id": "code-qwen", "kind": "openai", "url": "http://10.0.0.1:8082/",
     "key": "", "model": ""},
    {"id": "brainy", "kind": "openai", "url": "http://10.0.0.2:8087",
     "key": "sk-test", "model": "brain-26b"},
    {"id": "claude", "kind": "anthropic", "url": "https://api.anthropic.com",
     "key": "sk-ant", "model": "claude-sonnet-5"},
    {"id": "no-url", "kind": "openai", "url": ""},
]}
c = router._agent_coder("code-qwen")
check("openai backend resolves", bool(c), str(c))
check("url gets /v1/chat/completions (trailing slash ok)",
      c and c["url"] == "http://10.0.0.1:8082/v1/chat/completions", str(c))
check("empty key/model become None", c and c["key"] is None and c["model"] is None)
c2 = router._agent_coder("brainy")
check("key + model pass through",
      c2 and c2["key"] == "sk-test" and c2["model"] == "brain-26b", str(c2))
check("anthropic-kind REJECTED (agent speaks openai only)",
      router._agent_coder("claude") is None)
check("url-less backend rejected", router._agent_coder("no-url") is None)
check("unknown id rejected", router._agent_coder("nope") is None)

# ---- /agent/hosts lists coder ids (admin-gated) ------------------------------
print("--- /agent/hosts coders ---")
router.create_user("admin@x.com", "correct-horse-battery", is_admin=True)
adm = router.app.test_client()
adm.post("/login", data={"email": "admin@x.com", "password": "correct-horse-battery"})
h = adm.get("/agent/hosts").get_json()
ids = [x["id"] for x in h.get("coders", [])]
check("default is first", ids[:1] == ["default"], str(ids))
check("openai backends listed", "code-qwen" in ids and "brainy" in ids, str(ids))
check("anthropic + url-less EXCLUDED", "claude" not in ids and "no-url" not in ids, str(ids))
check("only ids reach the client — no url/key",
      all(set(x.keys()) == {"id"} for x in h.get("coders", [])), str(h.get("coders")))

# ---- /agent/run validates the coder before taking the run slot ---------------
print("--- /agent/run coder validation ---")
seen = {}
def fake_run(task, **kw):
    seen.update(kw)
    return {"done": True, "summary": "ok", "files": [], "steps": 1}
agent.run_agent = fake_run
r = adm.post("/agent/run", json={"task": "x", "coder": "nope"})
check("unknown coder -> 400", r.status_code == 400, str(r.get_json()))
check("run slot NOT leaked by the 400",
      router.AGENT_ACTIVE.acquire(blocking=False) and (router.AGENT_ACTIVE.release() or True))
r = adm.post("/agent/run", json={"task": "x", "coder": "brainy"})
check("valid coder accepted", r.status_code == 200, str(r.get_json()))
for _ in range(50):
    if seen: break
    time.sleep(0.1)
check("resolved coder handed to run_agent",
      seen.get("coder", {}).get("url") == "http://10.0.0.2:8087/v1/chat/completions"
      and seen.get("coder", {}).get("key") == "sk-test", str(seen.get("coder")))
seen.clear()
r = adm.post("/agent/run", json={"task": "x"})
check("no coder -> default still runs", r.status_code == 200, str(r.get_json()))
for _ in range(50):
    if seen: break
    time.sleep(0.1)
check("default coder resolves to the ROUTER'S code backend (zero-config)",
      (seen.get("coder") or {}).get("url") == router.CODE_AGENT_URL,
      f"{seen.get('coder')} vs {router.CODE_AGENT_URL}")

# ---- agent._chat honors the coder dict ---------------------------------------
print("--- _chat coder plumbing (real HTTP stub) ---")
msg = [{"role": "user", "content": "ping"}]
out = agent._chat(msg, coder={"url": BASE + "/v1/chat/completions",
                              "key": "sk-test", "model": "brain-26b"})
check("reply comes from the coder url", out == "pong", out)
check("bearer key sent", LAST.get("auth") == "Bearer sk-test", str(LAST.get("auth")))
check("model field sent when set", LAST.get("body", {}).get("model") == "brain-26b")
LAST.clear()
agent._chat(msg, coder={"url": BASE + "/v1/chat/completions"})
check("no key -> no auth header", LAST.get("auth") is None, str(LAST.get("auth")))
check("no model -> field omitted", "model" not in LAST.get("body", {}))
LAST.clear()
os.environ["CREWE_AGENT_URL"] = BASE + "/env/v1/chat/completions"
try:
    out = agent._chat(msg)
    check("coder=None falls back to CREWE_AGENT_URL — read PER CALL, not at import",
          out == "pong" and LAST.get("path") == "/env/v1/chat/completions",
          str(LAST.get("path")))
finally:
    del os.environ["CREWE_AGENT_URL"]

# ---- streaming: progress, cancel, fallback -----------------------------------
print("--- streaming ---")
prog = []
out = agent._chat(msg, coder={"url": BASE + "/sse/v1/chat/completions"},
                  on_progress=prog.append)
check("SSE chunks reassemble", out == "stream", out)
check("progress reported and monotonic",
      prog and prog[-1] == 6 and prog == sorted(prog), str(prog))
check("non-SSE backend still works (stream:true ignored)",
      agent._chat(msg, coder={"url": BASE + "/v1/chat/completions"}) == "pong")
t0 = time.time()
try:
    agent._chat(msg, coder={"url": BASE + "/drip/v1/chat/completions"},
                should_cancel=lambda: time.time() - t0 > 1.0)
    cancelled_fast = False
except agent.AgentCancelled:
    cancelled_fast = (time.time() - t0) < 4
check("cancel lands MID-generation, not after it (10s stream, ~1s stop)",
      cancelled_fast, f"{time.time()-t0:.1f}s")

print("--- observation clipping ---")
big = "A" * 9000 + "MIDDLE" + "B" * 9000
clipped = agent._clip_obs(big, limit=1000)
check("oversized obs clipped near the limit", len(clipped) < 1300, str(len(clipped)))
check("head and tail survive; middle cut",
      clipped.startswith("AAA") and clipped.endswith("BBB")
      and "MIDDLE" not in clipped and "omitted from the middle" in clipped)
check("small obs untouched", agent._clip_obs("tiny", limit=1000) == "tiny")

# ---- timeouts ----------------------------------------------------------------
print("--- timeouts ---")
check("silence timeout default 900 (streaming resets it per chunk)", agent.READ_TIMEOUT == 900,
      str(agent.READ_TIMEOUT))
check("cmd timeout default 600 (was 180)", agent.CMD_TIMEOUT == 600,
      str(agent.CMD_TIMEOUT))
# env override + real behavior: 1s read timeout against a 3s-slow stub must
# raise ReadTimeout (fresh interpreter so the module-level int is re-read)
code = (
    ("import sys; sys.path.insert(0, %r)\n" % ROOT)
    + "import requests, agent\n"
    "assert agent.READ_TIMEOUT == 1, agent.READ_TIMEOUT\n"
    "try:\n"
    f"    agent._chat([{{'role':'user','content':'x'}}], coder={{'url': '{BASE}/slow/v1/chat/completions'}})\n"
    "    print('NO_TIMEOUT')\n"
    "except requests.exceptions.ReadTimeout:\n"
    "    print('READ_TIMEOUT')\n"
)
env = dict(os.environ, CREWE_AGENT_READ_TIMEOUT="1")
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                   env=env, timeout=60)
check("CREWE_AGENT_READ_TIMEOUT honored (slow coder -> ReadTimeout)",
      r.stdout.strip() == "READ_TIMEOUT", r.stdout + r.stderr)

srv.shutdown()
print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
