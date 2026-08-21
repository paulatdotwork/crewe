"""Agent coder dialects: an Anthropic Messages backend drives the loop natively.

Why: the coder dropdown only listed openai-kind backends, so the strongest
configured coder (a Claude backend, kind "anthropic") could not be chosen for
the agent at all. agent.py now speaks both wire shapes; the router offers any
backend it can drive and resolves it server-side (id in, url/key never out).

Stubs a local HTTP server for both dialects — no network, no live models.
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
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


# ---- stub model server: both dialects ----------------------------------------
LAST = {}
LAST_GET = {}
HITS = {}


class Stub(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        payload = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _sse_head(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

    def _ev(self, t, obj):
        self.wfile.write((f"event: {t}\ndata: " + json.dumps(obj) + "\n\n").encode())
        self.wfile.flush()

    def do_GET(self):
        LAST_GET.update(path=self.path, auth=self.headers.get("Authorization"))
        if self.path == "/redir/props":
            self.send_response(302)
            self.send_header("Location", "/props")
            self.end_headers()
            return
        if self.path == "/props":
            return self._json(200, {"default_generation_settings": {"n_ctx": 32768},
                                    "total_slots": 1})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        LAST.update(path=self.path, headers={k.lower(): v for k, v in self.headers.items()},
                    body=json.loads(body or b"{}"))
        p = self.path
        HITS[p] = HITS.get(p, 0) + 1
        if p == "/drip/v1/messages":
            # a 4xx whose body never arrives — must not hold the caller
            self.send_response(400)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                self.wfile.flush()
                time.sleep(4)
                self.wfile.write(b"1\r\n{\r\n")
            except Exception:
                pass
            return
        if p == "/ratelimit/v1/messages" and HITS[p] == 1:
            payload = json.dumps({"type": "error", "error": {"type": "rate_limit_error",
                                                             "message": "slow down"}}).encode()
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if p == "/busy/v1/chat/completions" and HITS[p] == 1:
            return self._json(503, {"error": {"message": "no slot available"}})
        if p == "/bigcap/v1/messages" and LAST["body"].get("max_tokens", 0) > 8192:
            return self._json(400, {"type": "error", "error": {
                "type": "invalid_request_error",
                "message": "max_tokens: 32768 > 8192, which is the maximum allowed number of output tokens for this model"}})
        if p.endswith("/v1/chat/completions"):
            self._sse_head()
            for piece in ("st", "re", "am"):
                self.wfile.write(("data: " + json.dumps(
                    {"choices": [{"delta": {"content": piece}}]}) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        if p == "/json/v1/messages":
            return self._json(200, {"type": "message", "stop_reason": "end_turn",
                                    "content": [{"type": "text", "text": "po"},
                                                {"type": "tool_use", "name": "x"},
                                                {"type": "text", "text": "ng"}]})
        if p == "/refusal/v1/messages":
            return self._json(200, {"type": "message", "stop_reason": "refusal",
                                    "content": []})
        if p == "/err/v1/messages":
            return self._json(400, {"type": "error", "error": {
                "type": "invalid_request_error", "message": "max_tokens: too big"}})
        if p == "/refusalsse/v1/messages":
            self._sse_head()
            self._ev("message_start", {"type": "message_start",
                                       "message": {"usage": {"input_tokens": 1}}})
            self._ev("message_delta", {"type": "message_delta",
                                       "delta": {"stop_reason": "refusal"},
                                       "usage": {"output_tokens": 0}})
            self._ev("message_stop", {"type": "message_stop"})
            return
        if p == "/sseerr/v1/messages":
            self._sse_head()
            self._ev("message_start", {"type": "message_start",
                                       "message": {"usage": {"input_tokens": 1}}})
            self._ev("error", {"type": "error", "error": {"type": "overloaded_error",
                                                          "message": "overloaded"}})
            return
        if p.endswith("/v1/messages"):
            self._sse_head()
            self._ev("message_start", {"type": "message_start",
                                       "message": {"usage": {"input_tokens": 10}}})
            self._ev("content_block_start", {"type": "content_block_start", "index": 0,
                                             "content_block": {"type": "text", "text": ""}})
            self._ev("ping", {"type": "ping"})
            self._ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                             "delta": {"type": "thinking_delta",
                                                       "thinking": "hmm"}})
            for piece in ("st", "re", "am"):
                self._ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                 "delta": {"type": "text_delta",
                                                           "text": piece}})
            self._ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            self._ev("message_delta", {"type": "message_delta",
                                       "delta": {"stop_reason": "end_turn"},
                                       "usage": {"output_tokens": 5}})
            self._ev("message_stop", {"type": "message_stop"})
            return
        return self._json(404, {"error": "no such route"})

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

MSGS = [{"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
        {"role": "user", "content": "WORKSPACE"},
        {"role": "assistant", "content": "```json\n{}\n```"},
        {"role": "user", "content": "OBS 1"}]

# ---- body translation (pure) ---------------------------------------------------
print("--- _anthropic_body ---")
b = agent._anthropic_body(MSGS, 0.2, 32768, "claude-sonnet-5")
check("system prompt is a top-level string", b.get("system") == "SYS")
check("no role=system message survives",
      all(m["role"] in ("user", "assistant") for m in b["messages"]))
check("consecutive user turns merged so turns alternate",
      [m["role"] for m in b["messages"]] == ["user", "assistant", "user"],
      str([m["role"] for m in b["messages"]]))
check("merged content keeps both parts",
      b["messages"][0]["content"] == "TASK\n\nWORKSPACE")
check("max_tokens, model, stream set; NO sampling params (current Claude models 400 on temperature)",
      b["max_tokens"] == 32768 and b["model"] == "claude-sonnet-5"
      and b["stream"] is True and "temperature" not in b and "top_p" not in b, str(b.keys()))
b2 = agent._anthropic_body([{"role": "system", "content": "S"},
                            {"role": "assistant", "content": "lead"}], 0, 10, "m")
check("a conversation that would start with assistant gets a user opener",
      b2["messages"][0]["role"] == "user", str(b2["messages"]))
b4 = agent._anthropic_body([{"role": "user", "content": "u"},
                            {"role": "assistant", "content": ""},
                            {"role": "user", "content": "OBS"}], 0, 10, "m")
check("an empty assistant turn is named, not sent empty (the API 400s on it)",
      b4["messages"][1] == {"role": "assistant", "content": "(empty reply)"}, str(b4["messages"]))
b3 = agent._anthropic_body([{"role": "system", "content": "A"},
                            {"role": "system", "content": "B"},
                            {"role": "user", "content": "u"}], 0, 10, "m")
check("two system prompts join", b3["system"] == "A\n\nB")

# ---- streaming over the anthropic dialect ------------------------------------
print("--- _chat kind=anthropic ---")
CLAUDE = {"url": BASE + "/v1/messages", "kind": "anthropic", "key": "sk-ant",
          "model": "claude-sonnet-5"}
LAST.clear()
out = agent._chat(MSGS, coder=CLAUDE)
check("text deltas assembled in order", out == "stream", repr(out))
h = LAST.get("headers", {})
check("x-api-key + anthropic-version headers, no Bearer",
      h.get("x-api-key") == "sk-ant" and h.get("anthropic-version") == "2023-06-01"
      and "authorization" not in h, str(h))
body = LAST.get("body", {})
check("body is the Messages shape (system hoisted, alternating turns)",
      body.get("system") == "SYS"
      and [m["role"] for m in body["messages"]] == ["user", "assistant", "user"], str(body)[:300])
check("no llama.cpp-only kwargs sent to Anthropic", "chat_template_kwargs" not in body)
check("default cap is ANTHROPIC_MAX_TOKENS (a mid-file cap destroys a write)",
      body.get("max_tokens") == agent.ANTHROPIC_MAX_TOKENS == 32768, str(body.get("max_tokens")))
check("model name sent", body.get("model") == "claude-sonnet-5")
check("thinking deltas ignored (only text_delta counts)", "hmm" not in out)
check("planner cap (700) passes through untouched",
      agent._plan("make x", coder=CLAUDE) == "stream" and LAST["body"]["max_tokens"] == 700,
      str(LAST["body"].get("max_tokens")))

out = agent._chat(MSGS, coder=dict(CLAUDE, url=BASE + "/json/v1/messages"))
check("non-stream JSON reply: text blocks joined, other blocks skipped", out == "pong", repr(out))
out = agent._chat(MSGS, coder=dict(CLAUDE, url=BASE + "/refusal/v1/messages"))
check("an empty refusal reads as a decline, not an empty success",
      "declined" in out, repr(out))
out = agent._chat(MSGS, coder=dict(CLAUDE, url=BASE + "/refusalsse/v1/messages"))
check("a streamed refusal with no text reads as a decline too", "declined" in out, repr(out))
try:
    agent._chat(MSGS, coder=dict(CLAUDE, url=BASE + "/err/v1/messages"))
    check("HTTP 400 raises", False)
except requests.exceptions.HTTPError as e:
    check("HTTP 400 raises HTTPError carrying the server's reason",
          "400" in str(e) and "max_tokens: too big" in str(e), str(e))
except Exception as e:
    check("HTTP 400 raises HTTPError", False, repr(e))
try:
    agent._chat(MSGS, coder=dict(CLAUDE, url=BASE + "/sseerr/v1/messages"))
    check("stream error event raises", False)
except RuntimeError as e:
    check("stream error event raises with the message", "overloaded" in str(e), str(e))
except Exception as e:
    check("stream error event raises RuntimeError", False, repr(e))
t0 = time.time()
out = agent._chat(MSGS, coder=dict(CLAUDE, url=BASE + "/ratelimit/v1/messages"))
check("a 429 is retried after Retry-After and then succeeds",
      out == "stream" and HITS["/ratelimit/v1/messages"] == 2 and time.time() - t0 >= 1, f"{out!r} {time.time() - t0:.1f}s")
out = agent._chat(MSGS, coder=dict(CLAUDE, url=BASE + "/bigcap/v1/messages"))
check("a backend with a lower output ceiling: one retry at the classic 8192 cap",
      out == "stream" and LAST["body"]["max_tokens"] == 8192, str(LAST["body"].get("max_tokens")))
t0 = time.time()
try:
    agent._chat(MSGS, coder=dict(CLAUDE, url=BASE + "/drip/v1/messages"))
    check("a dripping 4xx body raises", False)
except requests.exceptions.HTTPError as e:
    check("a 4xx whose body never arrives raises at once (body read only when small and length-declared)",
          "400" in str(e) and time.time() - t0 < 2, f"{time.time() - t0:.1f}s {e}")
LAST.clear()
try:
    agent._chat(MSGS, coder=dict(CLAUDE, model=None))
    check("anthropic coder without a model refused", False)
except RuntimeError as e:
    check("anthropic coder without a model refused before any request",
          "model" in str(e) and not LAST, str(e) + str(LAST)[:80])

# ---- the openai path is byte-for-byte what it was ------------------------------
print("--- _chat kind=openai unchanged ---")
LAST.clear()
out = agent._chat(MSGS, coder={"url": BASE + "/v1/chat/completions", "key": "k"})
h, body = LAST.get("headers", {}), LAST.get("body", {})
check("openai streams as before", out == "stream", repr(out))
check("Bearer auth header", h.get("authorization") == "Bearer k", str(h))
check("system stays a message; thinking kwarg and 8192 cap as before",
      body["messages"][0]["role"] == "system"
      and body.get("chat_template_kwargs") == {"enable_thinking": False}
      and body.get("max_tokens") == 8192, str(body)[:200])
check("no model key when the backend has none", "model" not in body)
out = agent._chat(MSGS, coder={"url": BASE + "/v1/chat/completions", "kind": "weird"})
check("an unknown kind falls back to the openai dialect", out == "stream")
out = agent._chat(MSGS, coder={"url": BASE + "/busy/v1/chat/completions"})
check("a busy llama.cpp (503 no slot) is retried, not fatal",
      out == "stream" and HITS["/busy/v1/chat/completions"] == 2)
try:
    agent._chat(MSGS, coder={"url": BASE + "/missing"})
    check("openai HTTP error raises", False)
except requests.exceptions.HTTPError as e:
    check("openai HTTP 404 message carries the body", "no such route" in str(e), str(e))

# ---- context-window probe ---------------------------------------------------------
print("--- _probe_ctx ---")
check("llama.cpp-style /props -> n_ctx",
      agent._probe_ctx({"url": BASE + "/v1/chat/completions"}) == 32768)
check("a server without /props -> None (default budget applies)",
      agent._probe_ctx({"url": BASE + "/noprops/v1/chat/completions"}) is None)
check("the probe never follows redirects", agent._probe_ctx({"url": BASE + "/redir/v1/chat/completions"}) is None
      and LAST_GET.get("path") == "/redir/props", str(LAST_GET))
LAST_GET.clear()
check("anthropic coder is never probed", agent._probe_ctx(CLAUDE) is None and not LAST_GET)
check("no configured url -> no probe", agent._probe_ctx({}) is None and not LAST_GET)
LAST_GET.clear()
check("/props is asked with the backend's key when it has one",
      agent._probe_ctx({"url": BASE + "/v1/chat/completions", "key": "k2"}) == 32768
      and LAST_GET.get("auth") == "Bearer k2", str(LAST_GET))
os.environ["CREWE_AGENT_URL"] = BASE + "/v1/chat/completions"
check("CREWE_AGENT_URL counts as configured", agent._probe_ctx(None) == 32768)
os.environ.pop("CREWE_AGENT_URL", None)
check("budget follows the window: 8192 tokens -> 19660 chars",
      agent._ctx_budget(8192) == 19660 and agent._ctx_budget(None) == agent.CTX_CHARS)

# ---- router: an anthropic backend is a valid agent coder --------------------------
print("--- router coder selection ---")
tmp = tempfile.mkdtemp()
router.USERS_FILE = os.path.join(tmp, "u.json")
router.USERDATA_ROOT = os.path.join(tmp, "d")
router.app.config["SESSION_COOKIE_SECURE"] = False
router.AGENT_BROWSER_CHECK = False
router.ROUTER_CONFIG = {"backends": [
    {"id": "code-qwen", "kind": "openai", "url": "http://10.0.0.1:8082/", "key": "", "model": ""},
    {"id": "claude", "kind": "anthropic", "url": "https://api.anthropic.com", "key": "sk-ant",
     "model": "claude-sonnet-5"},
    {"id": "claude-nomodel", "kind": "anthropic", "url": "https://api.anthropic.com",
     "key": "sk-ant", "model": ""},
    {"id": "llama-raw", "kind": "llamacpp", "url": "http://10.0.0.3:8080", "model": "m"},
    {"id": "ollama", "kind": "ollama", "url": "http://10.0.0.4:11434", "model": "m"},
]}
c = router._agent_coder("claude")
check("anthropic backend resolves with kind, /v1/messages url, key, model",
      c == {"url": "https://api.anthropic.com/v1/messages", "key": "sk-ant",
            "model": "claude-sonnet-5", "kind": "anthropic"}, str(c))
check("anthropic backend WITHOUT a model rejected",
      router._agent_coder("claude-nomodel") is None)
check("llamacpp/ollama kinds still rejected",
      router._agent_coder("llama-raw") is None and router._agent_coder("ollama") is None)
c = router._agent_coder("code-qwen")
check("openai backend resolves as before, now with kind",
      c == {"url": "http://10.0.0.1:8082/v1/chat/completions", "key": None,
            "model": None, "kind": "openai"}, str(c))

router.create_user("admin@x.com", "correct-horse-battery", is_admin=True)
adm = router.app.test_client()
adm.post("/login", data={"email": "admin@x.com", "password": "correct-horse-battery"})
h = adm.get("/agent/hosts").get_json()
coders = h.get("coders", [])
ids = [x["id"] for x in coders]
check("/agent/hosts lists the anthropic backend, not the model-less or other kinds",
      "claude" in ids and "claude-nomodel" not in ids and "llama-raw" not in ids
      and "ollama" not in ids, str(ids))
lab = next((x.get("label") for x in coders if x["id"] == "claude"), None)
check("label names the model (never a host/key)", lab == "claude · claude-sonnet-5", str(lab))
check("only ids/labels reach the client — no url, host or key",
      all(set(x.keys()) <= {"id", "label"} for x in coders)
      and not any(t in json.dumps(coders) for t in ("http", "://", "sk-", ".0.0.", ":8", ":9")),
      json.dumps(coders))

# default coder: an anthropic coder_fast WITH a model is usable; without one it is skipped
os.environ.pop("CREWE_AGENT_URL", None)
router.CODE_AGENT_URL_FAST = "https://api.anthropic.com/v1/messages"
router.BACKEND_INFO["https://api.anthropic.com/v1/messages"] = {
    "kind": "anthropic", "model": "claude-sonnet-5", "key": "sk-ant"}
router.SPECIALISTS["code"] = "http://10.0.0.1:8082/v1/chat/completions"
router.BACKEND_INFO["http://10.0.0.1:8082/v1/chat/completions"] = {"kind": "openai"}
d = router._agent_default_coder()
check("default resolves to an anthropic coder_fast that has a model",
      d and d["kind"] == "anthropic" and d["model"] == "claude-sonnet-5", str(d))
check("default id shown to the UI is the backend id",
      router._agent_default_coder_id() == "claude", str(router._agent_default_coder_id()))
router.BACKEND_INFO["https://api.anthropic.com/v1/messages"]["model"] = ""
d = router._agent_default_coder()
check("a model-less anthropic tier is skipped for the next usable coder",
      d and d["kind"] == "openai" and d["url"].endswith(":8082/v1/chat/completions"), str(d))

# /agent/run hands the resolved anthropic coder to run_agent
seen = {}
def fake_run(task, **kw):
    seen.update(kw)
    return {"done": True, "summary": "ok", "files": [], "steps": 1}
agent.run_agent = fake_run
r = adm.post("/agent/run", json={"task": "x", "coder": "claude"})
check("run with the anthropic coder accepted", r.status_code == 200, str(r.get_json()))
import time
for _ in range(50):
    if seen:
        break
    time.sleep(0.1)
check("run_agent receives kind=anthropic with the /v1/messages url",
      seen.get("coder", {}).get("kind") == "anthropic"
      and seen.get("coder", {}).get("url") == "https://api.anthropic.com/v1/messages",
      str(seen.get("coder")))
r = adm.post("/agent/run", json={"task": "x", "coder": "claude-nomodel"})
check("model-less anthropic coder -> 400", r.status_code == 400, str(r.get_json()))

print(f"\n{len(ok)} passed, {len(bad)} failed")
if bad:
    print("FAILED:", bad)
sys.exit(1 if bad else 0)
