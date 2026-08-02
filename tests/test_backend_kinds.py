"""Backend kinds: OpenAI-compatible, llama.cpp, Ollama, and native Claude.

The end-to-end tests run a MOCK ANTHROPIC SERVER that speaks the real wire
protocol — it asserts on what Crewe *sends* (path, headers, body shape) and
replies with a genuine Anthropic SSE stream. That verifies the whole path
without an API key, and would catch a regression a unit test on the translator
alone would miss.
"""
import os, sys, json, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router

tmpdir = tempfile.mkdtemp()
router.USERS_FILE = os.path.join(tmpdir, "users.json")
router.app.config["SESSION_COOKIE_SECURE"] = False
import shutil as _sh
_cfgcopy = os.path.join(tmpdir, "router_config.json")
if os.path.exists(router.ROUTER_CONFIG_FILE):
    _sh.copyfile(router.ROUTER_CONFIG_FILE, _cfgcopy)
else:                       # fresh clone: no config yet -> built-in defaults
    json.dump(router.ROUTER_CONFIG, open(_cfgcopy, "w"))
router.ROUTER_CONFIG_FILE = _cfgcopy

P, F = [], []
def check(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (f"   [{detail}]" if detail and not cond else ""))

# --------------------------------------------------------------- kind basics
print("--- kinds and URLs ---")
check("unknown kind falls back to openai",
      router._kind({"kind": "nonsense"}) == "openai")
check("absent kind falls back to openai (pre-kinds configs)",
      router._kind({}) == "openai")
for kind, want in [("openai", "/v1/chat/completions"),
                   ("llamacpp", "/v1/chat/completions"),
                   ("ollama", "/v1/chat/completions"),
                   ("anthropic", "/v1/messages")]:
    u = router._chat_url({"url": "http://h", "kind": kind})
    check(f"{kind:9} -> {want}", u.endswith(want), u)
check("a trailing slash in the base URL doesn't double up",
      router._chat_url({"url": "http://h/", "kind": "anthropic"}) ==
      "http://h/v1/messages")

print("--- auth headers ---")
A = "http://anth/v1/messages"
O = "http://oai/v1/chat/completions"
router.BACKEND_INFO[A] = {"key": "sk-ant-x", "model": "claude-opus-5", "kind": "anthropic"}
router.BACKEND_INFO[O] = {"key": "sk-oai-x", "model": "gpt", "kind": "openai"}
h = router._hdrs(A)
check("anthropic sends x-api-key", h.get("x-api-key") == "sk-ant-x", str(h))
check("anthropic sends anthropic-version",
      h.get("anthropic-version") == router.ANTHROPIC_VERSION, str(h))
check("anthropic does NOT send Authorization (a 401 with a confusing message)",
      "Authorization" not in h, str(h))
check("openai still sends bearer auth",
      router._hdrs(O).get("Authorization") == "Bearer sk-oai-x")

# -------------------------------------------------------- request translation
print("--- request translation ---")
t = router._to_anthropic({
    "model": "claude-opus-5", "stream": True, "temperature": 0.2, "top_p": 0.9,
    "top_k": 40, "chat_template_kwargs": {"enable_thinking": False},
    "stream_options": {"include_usage": True},
    "messages": [{"role": "system", "content": "SYS-A"},
                 {"role": "user", "content": "hello"},
                 {"role": "assistant", "content": "hi"},
                 {"role": "system", "content": "SYS-B"},
                 {"role": "user", "content": "again"}]})
check("system messages are lifted to a top-level string",
      t.get("system") == "SYS-A\n\nSYS-B", repr(t.get("system")))
check("no system message survives in messages[]",
      all(m["role"] != "system" for m in t["messages"]), str(t["messages"]))
check("user/assistant order is preserved",
      [m["role"] for m in t["messages"]] == ["user", "assistant", "user"],
      str([m["role"] for m in t["messages"]]))
for bad in ("temperature", "top_p", "top_k", "chat_template_kwargs", "stream_options"):
    check(f"{bad} is stripped (a 400 on current Claude models)", bad not in t)
check("max_tokens is added when absent (Anthropic requires it)",
      t["max_tokens"] == router.ANTHROPIC_DEFAULT_MAX_TOKENS, str(t.get("max_tokens")))
check("stream survives", t.get("stream") is True)

check("max_tokens=0 (Crewe's 'uncapped') becomes a real number",
      router._to_anthropic({"messages": [{"role": "user", "content": "x"}],
                            "max_tokens": 0})["max_tokens"] > 0)
check("an explicit max_tokens is respected",
      router._to_anthropic({"messages": [{"role": "user", "content": "x"}],
                            "max_tokens": 55})["max_tokens"] == 55)
lead = router._to_anthropic({"messages": [{"role": "assistant", "content": "a"},
                                          {"role": "user", "content": "u"}]})
check("a leading assistant turn is dropped (must start with user)",
      lead["messages"][0]["role"] == "user", str(lead["messages"]))
only_sys = router._to_anthropic({"messages": [{"role": "system", "content": "s"}]})
check("a system-only conversation still produces a legal body",
      only_sys["messages"] and only_sys["messages"][0]["role"] == "user",
      str(only_sys))
check("stream_options is not added for anthropic even when paid",
      "stream_options" not in router._with_usage(
          {"messages": [], "stream": True}, A))

# ------------------------------------------------------- response translation
print("--- SSE translation ---")
def sse(*events):
    return [f"data: {json.dumps(e)}".encode() for e in events]

out = list(router._anthropic_to_openai_lines(sse(
    {"type": "message_start", "message": {"usage": {"input_tokens": 11}}},
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}},
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
     "usage": {"output_tokens": 7}},
    {"type": "message_stop"})))
chunks = [json.loads(l[6:]) for l in out if l != b"data: [DONE]"]
text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
check("text deltas concatenate to the full answer", text == "Hello", repr(text))
check("the stream terminates with [DONE]", out[-1] == b"data: [DONE]")
usage = next((c["usage"] for c in chunks if c.get("usage")), None)
check("input tokens survive from message_start",
      usage and usage["prompt_tokens"] == 11, str(usage))
check("output tokens survive from message_delta",
      usage and usage["completion_tokens"] == 7, str(usage))
check("stop_reason becomes finish_reason",
      any(c["choices"][0].get("finish_reason") == "end_turn" for c in chunks))

think = [json.loads(l[6:]) for l in router._anthropic_to_openai_lines(sse(
    {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}}))]
check("thinking deltas map to reasoning_content (keeps the token counter moving)",
      think[0]["choices"][0]["delta"].get("reasoning_content") == "hmm", str(think))

ref = [json.loads(l[6:]) for l in router._anthropic_to_openai_lines(sse(
    {"type": "message_delta", "delta": {"stop_reason": "refusal"}, "usage": {}}))
    if l != b"data: [DONE]"]
check("a refusal produces visible text, not a silent empty answer",
      any("declined" in c["choices"][0]["delta"].get("content", "") for c in ref),
      str(ref))
err = [json.loads(l[6:]) for l in router._anthropic_to_openai_lines(sse(
    {"type": "error", "error": {"message": "overloaded"}}))]
check("a mid-stream error surfaces to the user",
      "overloaded" in err[0]["choices"][0]["delta"].get("content", ""), str(err))
check("junk lines are skipped, not fatal",
      list(router._anthropic_to_openai_lines([b"", b": ping", b"data: {oops"])) == [])

# ------------------------------------------------- end-to-end via mock server
print("--- end to end against a mock Anthropic server ---")
seen = {}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/v1/models":
            body = json.dumps({"data": [{"id": "claude-opus-5",
                                         "max_input_tokens": 900000}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        seen["path"] = self.path
        seen["headers"] = dict(self.headers)
        seen["body"] = json.loads(self.rfile.read(n) or b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for e in ({"type": "message_start", "message": {"usage": {"input_tokens": 21}}},
                  {"type": "content_block_delta",
                   "delta": {"type": "text_delta", "text": "mocked "}},
                  {"type": "content_block_delta",
                   "delta": {"type": "text_delta", "text": "claude reply"}},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                   "usage": {"output_tokens": 9}},
                  {"type": "message_stop"}):
            self.wfile.write(b"event: x\r\n" + b"data: " + json.dumps(e).encode() + b"\r\n\r\n")
            self.wfile.flush()

srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
MOCK = f"http://127.0.0.1:{port}/v1/messages"
router.BACKEND_INFO[MOCK] = {"key": "sk-ant-test", "model": "claude-opus-5",
                             "kind": "anthropic", "paid": True,
                             "price_in": 5.0, "price_out": 25.0}
router.JOBS["k1"] = {"owner": "u", "tokens": 0, "done": False, "stage": "x",
                     "stage_ts": 0}
try:
    text = router._stream_chat(
        MOCK,
        [{"role": "system", "content": "be brief"},
         {"role": "user", "content": "hello claude"}],
        "k1", temperature=0.2, max_tokens=100, timeout=30)
    check("the answer streams back through the whole pipeline",
          text.strip() == "mocked claude reply", repr(text))
    check("Crewe POSTs to /v1/messages", seen.get("path") == "/v1/messages",
          str(seen.get("path")))
    hh = {k.lower(): v for k, v in (seen.get("headers") or {}).items()}
    check("x-api-key reached the server", hh.get("x-api-key") == "sk-ant-test", str(hh.get("x-api-key")))
    check("anthropic-version reached the server",
          hh.get("anthropic-version") == router.ANTHROPIC_VERSION)
    check("no Authorization header was sent", "authorization" not in hh)
    b = seen.get("body") or {}
    check("system prompt arrived top-level", b.get("system") == "be brief", str(b.get("system")))
    check("no system role in the wire messages",
          all(m["role"] != "system" for m in b.get("messages", [])), str(b.get("messages")))
    check("temperature was stripped from the wire body", "temperature" not in b, str(b))
    check("max_tokens was present on the wire", b.get("max_tokens") == 100, str(b.get("max_tokens")))
    check("the live token counter moved", router.JOBS["k1"]["tokens"] > 0,
          str(router.JOBS["k1"]["tokens"]))

    print("--- discovery against the mock ---")
    check("context window is read from /v1/models max_input_tokens",
          router._backend_ctx(MOCK) == 900000, str(router._backend_ctx(MOCK)))
    check("liveness works without /health", router._backend_alive(MOCK) is True)
finally:
    srv.shutdown()

# ------------------------------------------------------------- config surface
print("--- settings validation ---")
admin, err = router.create_user("admin@example.com", "correct-horse-battery", is_admin=True)
assert admin, err
c = router.app.test_client()
c.post("/login", data={"email": "admin@example.com", "password": "correct-horse-battery"})

cfg = json.loads(json.dumps(router.ROUTER_CONFIG))
# A unique id — the operator's real config is the test's starting point and
# may already contain a backend named "claude".
cfg["backends"] = [b for b in cfg["backends"] if b["id"] != "kindtest-claude"]
cfg["backends"].append({"id": "kindtest-claude", "url": "https://api.anthropic.com",
                        "kind": "anthropic", "key": "", "model": ""})
r = c.post("/settings/config", json=cfg)
check("an Anthropic backend with no model name is rejected",
      r.status_code == 400 and "model" in r.get_data(as_text=True),
      r.get_data(as_text=True)[:160])
cfg["backends"][-1]["model"] = "claude-opus-5"
r = c.post("/settings/config", json=cfg)
check("...and accepted once a model is named", r.status_code == 200,
      r.get_data(as_text=True)[:160])
cfg["backends"][-1]["kind"] = "bogus"
r = c.post("/settings/config", json=cfg)
saved = json.load(open(router.ROUTER_CONFIG_FILE))
check("an unknown kind is coerced to openai, not rejected",
      r.status_code == 200 and
      next(b for b in saved["backends"] if b["id"] == "kindtest-claude")["kind"] == "openai",
      r.get_data(as_text=True)[:120])
check("existing backends kept their default kind",
      all(b.get("kind", "openai") in router.BACKEND_KINDS for b in saved["backends"]))

print("--- rejections must teach, not just discard (regression) ---")
# An unlabeled block was rejected SILENTLY, so the model repeated the mistake
# twice in one step. Each guard now writes the lesson into working memory.
mem = router._mem_new("q", False)
w = []
router._merge_output_file("file1.txt", "let a = 1;", {"index.html": "<html>"},
                          {"index.html"}, w, "step 4", mem=mem)
check("unlabeled block rejection lands in working memory",
      any("filename line" in e for e in mem["events"]), str(mem["events"]))
mem2 = router._mem_new("q", False)
w2 = []
router._merge_output_file("notes.txt", "// just a comment", {}, set(), w2,
                          "step 4", mem=mem2)
check("echoed-example rejection lands in working memory",
      any("format example" in e for e in mem2["events"]), str(mem2["events"]))
mem3 = router._mem_new("q", False)
w3 = []
router._merge_output_file("ghost.js", "<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE",
                          {"index.html": "x"}, {"index.html"}, w3, "step 4", mem=mem3)
check("edits-to-nonexistent-file rejection lands in working memory",
      any("written IN FULL" in e for e in mem3["events"]), str(mem3["events"]))

print("--- autosized budgets are capped (a 1M-ctx paid model is not a license) ---")
b, n = router._budgets_for(MOCK)      # mock reports 900,000-token context
check("a huge context window caps at BUDGET_CAP_CHARS",
      b["small_project_chars"] == router.BUDGET_CAP_CHARS,
      str(b["small_project_chars"]))
check("the anthropic output ceiling is above the mid-file danger zone",
      router.ANTHROPIC_DEFAULT_MAX_TOKENS >= 32768,
      str(router.ANTHROPIC_DEFAULT_MAX_TOKENS))

print("--- language coverage (regression: a correct C# reply was discarded) ---")
CSHARP = """HelloWorld.csproj
```xml
<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType></PropertyGroup></Project>
```

Program.cs
```csharp
Console.WriteLine("Hello, world!");
```
"""
got = router._extract_files(CSHARP)
check("a C# project's real filenames are recovered",
      set(got) == {"HelloWorld.csproj", "Program.cs"}, str(list(got)))
check("no generated placeholder names leak in",
      not any(n.startswith(("index.", "file2.")) for n in got), str(list(got)))
for ext in ("cs", "csproj", "java", "go", "rb", "php", "swift", "kt"):
    check(f".{ext} is recognised as project source", ext in router.CODE_EXTS)
for tag, ext in (("csharp", "cs"), ("golang", "go"), ("ruby", "rb"),
                 ("kotlin", "kt"), ("c#", "cs")):
    check(f"fence tag ```{tag} maps to .{ext}", router._EXT_BY_LANG.get(tag) == ext,
          str(router._EXT_BY_LANG.get(tag)))
check("web/python extensions still recognised (no regression)",
      {"html", "js", "py", "rs"} <= router.CODE_EXTS)

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
