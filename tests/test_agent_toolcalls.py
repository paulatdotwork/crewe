"""Native tool calling: a capable coder drives the loop with OpenAI tool_calls.

Why: a model good at tool calling (a strong local model, or Claude) emits well-formed
tool_calls, a more robust channel than regex-parsing a ```json block out of
prose. The text protocol stays the universal fallback — a model that ignores
the tools, or a backend that rejects the `tools` param, still works. One
observation per action either way; the syntax checks, finish gate and verified
verdict are unchanged (they run through the SAME _do_action as the text path).

Two layers, both offline:
  - the wire: a stub server streams tool_call fragments; _chat assembles them.
  - the loop: _chat is scripted to return tool replies; the workspace is a stub.
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

_AUDIT_TMP = tempfile.mkdtemp(prefix="crewe-audit-test-")
os.environ["CREWE_AGENT_AUDIT_DIR"] = _AUDIT_TMP
os.environ.pop("CREWE_AGENT_URL", None)
os.environ.pop("CREWE_AGENT_TOOLS", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)
import agent

ok, bad = [], []


def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


# ============================================================ the wire =========
LAST = {}


class Stub(BaseHTTPRequestHandler):
    def _sse(self, chunks):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in chunks:
            self.wfile.write(("data: " + json.dumps(c) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")

    def _tc(self, idx, id=None, name=None, args=None):
        d = {"index": idx}
        if id:
            d["id"] = id
        fn = {}
        if name is not None:
            fn["name"] = name
        if args is not None:
            fn["arguments"] = args
        if fn:
            d["function"] = fn
        return {"choices": [{"delta": {"tool_calls": [d]}}]}

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        LAST.update(path=self.path, body=json.loads(body or b"{}"))
        p = self.path
        if p == "/one/v1/chat/completions":
            return self._sse([
                {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
                self._tc(0, id="call_a", name="write", args=""),
                self._tc(0, args='{"path": "a.py", '),
                self._tc(0, args='"content": "print(1)\\n"}'),
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ])
        if p == "/two/v1/chat/completions":
            return self._sse([
                self._tc(0, id="c0", name="write", args='{"path":"a.py","content":"1"}'),
                self._tc(1, id="c1", name="run", args='{"cmd":"python3 a.py"}'),
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ])
        if p == "/text/v1/chat/completions":
            txt = json.dumps({"action": "run", "cmd": "ls"})
            return self._sse([
                {"choices": [{"delta": {"content": "```json\n"}}]},
                {"choices": [{"delta": {"content": txt}}]},
                {"choices": [{"delta": {"content": "\n```"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ])
        if p == "/nonstream/v1/chat/completions":
            payload = json.dumps({"choices": [{"message": {"role": "assistant",
                "content": None, "tool_calls": [{"id": "z", "type": "function",
                "function": {"name": "run", "arguments": {"cmd": "ls"}}}]},
                "finish_reason": "tool_calls"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def coder(path):
    return {"url": BASE + path + "/v1/chat/completions", "kind": "openai"}


print("--- _toolcall_to_action / _norm_tool_calls ---")
a, err = agent._toolcall_to_action({"name": "write", "arguments": '{"path":"x","content":"y"}'})
check("json arguments -> action dict", a == {"action": "write", "path": "x", "content": "y"} and err is None, str(a))
a, err = agent._toolcall_to_action({"name": "run", "arguments": {"cmd": "ls"}})
check("dict arguments accepted", a == {"action": "run", "cmd": "ls"} and err is None, str(a))
a, err = agent._toolcall_to_action({"name": "write", "arguments": "{not json"})
check("bad JSON -> error fed back, action carries just the name",
      a == {"action": "write"} and err and "valid JSON" in err, str((a, err)))
a, err = agent._toolcall_to_action({"name": "run", "arguments": "[1,2]"})
check("non-object arguments -> empty args", a == {"action": "run"} and err is None, str(a))
n = agent._norm_tool_calls([{"id": "i", "function": {"name": "run", "arguments": {"cmd": "ls"}}}])
check("_norm_tool_calls stringifies dict arguments",
      n == [{"id": "i", "name": "run", "arguments": '{"cmd": "ls"}'}], str(n))

print("--- _chat assembles a streamed tool call ---")
r = agent._chat([{"role": "user", "content": "x"}], coder=coder("/one"), tools=agent.AGENT_TOOLS)
check("returns {content, tool_calls}", isinstance(r, dict) and "tool_calls" in r, str(type(r)))
tc = r["tool_calls"]
check("one call, id+name+assembled arguments",
      len(tc) == 1 and tc[0]["id"] == "call_a" and tc[0]["name"] == "write"
      and json.loads(tc[0]["arguments"]) == {"path": "a.py", "content": "print(1)\n"},
      str(tc))
check("the request carried tools + tool_choice",
      LAST["body"].get("tools") and LAST["body"].get("tool_choice") == "auto"
      and any(t["function"]["name"] == "finish" for t in LAST["body"]["tools"]), str(LAST["body"].get("tool_choice")))
r = agent._chat([{"role": "user", "content": "x"}], coder=coder("/two"), tools=agent.AGENT_TOOLS)
check("two batched calls assembled in index order",
      [c["name"] for c in r["tool_calls"]] == ["write", "run"]
      and json.loads(r["tool_calls"][1]["arguments"]) == {"cmd": "python3 a.py"}, str(r["tool_calls"]))
r = agent._chat([{"role": "user", "content": "x"}], coder=coder("/nonstream"), tools=agent.AGENT_TOOLS)
check("non-streamed tool_calls also returned", r["tool_calls"] and r["tool_calls"][0]["name"] == "run", str(r))
r = agent._chat([{"role": "user", "content": "x"}], coder=coder("/text"), tools=agent.AGENT_TOOLS)
check("a model that answers in text -> empty tool_calls, content carries the block",
      r["tool_calls"] == [] and "action" in r["content"], str(r)[:120])
r = agent._chat([{"role": "user", "content": "x"}], coder=coder("/text"))
check("without tools=, _chat still returns a bare string (text mode unchanged)", isinstance(r, str))


# ============================================================ the loop =========
class StubBackend:
    def __init__(self, files=None, exits=None):
        self.files = dict(files or {})
        self.ran = []
        self.exits = dict(exits or {})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cmd, timeout=None):
        self.ran.append(cmd)
        return self.exits.get(cmd, 0), f"ran:{cmd}", ""

    def write_file(self, path, content):
        self.files[path] = content

    def read_file(self, path, max_bytes=200_000):
        return self.files.get(path)

    def list_files(self, limit=200):
        return sorted(self.files)


def tool(name, **args):
    return {"id": f"id_{name}_{len(args)}", "name": name, "arguments": json.dumps(args)}


def scripted(replies):
    """Each reply: a list of tool dicts (tool turn), or a str (text turn)."""
    seen = {"calls": [], "n": 0, "tools_seen": []}

    def fake_chat(messages, tools=None, **kw):
        seen["calls"].append([dict(m) for m in messages])
        seen["tools_seen"].append(tools is not None)
        seen["n"] += 1
        r = replies[min(seen["n"] - 1, len(replies) - 1)]
        if isinstance(r, str):
            return r
        return {"content": "", "tool_calls": list(r)}

    agent._chat = fake_chat
    return seen


def run(replies, backend=None, max_steps=8, **kw):
    seen = scripted(replies)
    events = []
    res = agent.run_agent("do the task", max_steps=max_steps, plan=False,
                          backend=backend or StubBackend(),
                          on_event=events.append,
                          coder={"url": "http://x/v1/chat/completions", "kind": "openai"},
                          **kw)
    return res, seen, events


def msgs_of(seen, i=-1):
    return seen["calls"][i]


print("--- the loop uses tools for an openai coder ---")
res, seen, events = run([[tool("write", path="a.py", content="print('hi')\n")],
                         [tool("run", cmd="python3 a.py")],
                         [tool("finish", summary="done")]])
check("tools were offered to the coder", all(seen["tools_seen"]), str(seen["tools_seen"]))
sysmsg = seen["calls"][0][0]
check("the tool-mode system prompt is used", sysmsg["role"] == "system" and "calling the" in sysmsg["content"], sysmsg["content"][:60])
check("the run completes and is verified",
      res["done"] and res["verified"] and res["steps"] == 3, str({k: res.get(k) for k in ("done", "verified", "steps")}))
ch = [e for e in events if e.get("t") == "harness" and e.get("note", "").startswith("action channel")]
check("the channel is announced as native tool calls", ch and "native tool calls" in ch[0]["note"], str(ch))

# message shape after step 1: assistant(tool_calls) then tool(result)
hist = seen["calls"][1]
asst = [m for m in hist if m["role"] == "assistant"][-1]
toolm = [m for m in hist if m["role"] == "tool"][-1]
check("assistant turn carries a well-formed tool_calls array",
      asst.get("tool_calls") and asst["tool_calls"][0]["type"] == "function"
      and asst["tool_calls"][0]["function"]["name"] == "write", str(asst.get("tool_calls"))[:120])
check("the write's result came back as a tool message with the matching id",
      toolm["tool_call_id"] == asst["tool_calls"][0]["id"] and "wrote a.py" in toolm["content"]
      and "syntax OK" in toolm["content"], str(toolm)[:120])
check("a step trailer states the budget", any(m["role"] == "user" and "action(s) left" in (m["content"] or "")
                                              for m in hist), "no trailer")

print("--- syntax check + finish gate work through tool calls ---")
res, seen, events = run([[tool("write", path="m.py", content="def f(:\n")],  # syntax error
                         [tool("edit", path="m.py", find="def f(:", replace="def f():\n    pass")],
                         [tool("run", cmd="python3 m.py")],
                         [tool("finish", summary="fixed")]])
step1_tool = [m for m in seen["calls"][1] if m["role"] == "tool"][-1]
check("a syntax error is reported on the write's own tool result",
      "SYNTAX" in step1_tool["content"].upper(), step1_tool["content"][:80])
check("the run finishes verified after the fix + run", res["done"] and res["verified"], str(res.get("verify_note")))

res, seen, events = run([[tool("write", path="x.py", content="print(1)\n")],
                         [tool("finish", summary="done")],   # nothing run yet -> refused
                         [tool("run", cmd="python3 x.py")],
                         [tool("finish", summary="ran it")]])
refusal = [m for m in seen["calls"][2] if m["role"] == "tool"][-1]
check("finish is REFUSED (as a tool result) when code changed but nothing ran",
      "NOT FINISHED" in refusal["content"] and "finish refused" in refusal["content"], refusal["content"][:80])
check("after running, finish is accepted and verified", res["done"] and res["verified"] and res["steps"] == 4)
check("a finish refusal is visible in the transcript",
      any(e.get("t") == "warn" and "finish refused" in e.get("note", "") for e in events))

print("--- batched calls in one reply all execute ---")
res, seen, events = run([[tool("write", path="a.py", content="print(1)\n"),
                          tool("write", path="b.py", content="print(2)\n"),
                          tool("run", cmd="ls")],
                         [tool("finish", summary="both")]], backend=StubBackend())
hist = seen["calls"][1]
tools_back = [m for m in hist if m["role"] == "tool"]
check("three batched calls -> three tool results in order",
      len(tools_back) == 3 and "wrote a.py" in tools_back[0]["content"]
      and "wrote b.py" in tools_back[1]["content"] and "ran:ls" in tools_back[2]["content"], str(len(tools_back)))
check("one assistant turn held all three tool_calls",
      len([m for m in hist if m["role"] == "assistant" and m.get("tool_calls")]) == 1)

print("--- mid-run note defers a tool-mode finish ---")
pull = iter([[], ["also write c.py"], [], [], []])
calls = {"n": 0}
def pull_notes():
    calls["n"] += 1
    return ["also write c.py"] if calls["n"] == 2 else []
res, seen, events = run([[tool("write", path="a.py", content="print(1)\n")],
                         [tool("finish", summary="done")],       # note arrives -> deferred
                         [tool("run", cmd="python3 a.py")],
                         [tool("finish", summary="did the note too")]],
                        pull_notes=pull_notes)
deferral = [m for m in seen["calls"][2] if m["role"] == "tool"][-1]
check("the queued note defers the finish with a tool result naming it",
      "NOT FINISHED" in deferral["content"] and "also write c.py" in deferral["content"], deferral["content"][:100])
check("then the run finishes", res["done"])

print("--- bad-JSON tool arguments are fed back, not crashed ---")
res, seen, events = run([[{"id": "bad", "name": "write", "arguments": "{oops"}],
                         [tool("write", path="a.py", content="print(1)\n")],
                         [tool("run", cmd="python3 a.py")],
                         [tool("finish", summary="ok")]])
badres = [m for m in seen["calls"][1] if m["role"] == "tool"][-1]
check("a malformed-arguments call returns an error result, run continues",
      "valid JSON" in badres["content"] and res["done"], badres["content"][:80])

print("--- graceful fallback when the backend rejects tools ---")
class Rejects:
    def __init__(self):
        self.n = 0

    def __call__(self, messages, tools=None, **kw):
        self.n += 1
        if tools is not None and self.n == 1:
            resp = requests.Response()
            resp.status_code = 400
            resp._content = b'{"error":{"message":"tools are not supported by this model"}}'
            raise requests.exceptions.HTTPError("400 Bad Request — tools not supported", response=resp)
        # text mode from here
        return '```json\n{"action":"finish","summary":"text mode"}\n```'
agent._chat = Rejects()
events = []
res = agent.run_agent("do it", max_steps=5, plan=False, backend=StubBackend(),
                      on_event=events.append,
                      coder={"url": "http://x/v1/chat/completions", "kind": "openai"})
check("a tools-rejected 400 drops to the text protocol and the run still finishes",
      res["done"] and any(e.get("t") == "harness" and "rejected tool calling" in e.get("note", "") for e in events),
      str([e.get("note") for e in events if e.get("t") == "harness"]))

print("--- an anthropic coder never gets tools (text protocol) ---")
res, seen, events = run(['```json\n{"action":"finish","summary":"claude"}\n```'])
# override: rerun with an anthropic coder
seen = scripted(['```json\n{"action":"finish","summary":"claude"}\n```'])
events = []
res = agent.run_agent("do it", max_steps=3, plan=False, backend=StubBackend(),
                      on_event=events.append, coder={"url": "http://x/v1/messages", "kind": "anthropic", "model": "claude-x"})
check("anthropic coder: tools never offered, text protocol announced",
      seen["tools_seen"] == [False] and res["done"]
      and any("text protocol" in e.get("note", "") for e in events), str(seen["tools_seen"]))

print("--- CREWE_AGENT_TOOLS=0 forces text mode ---")
import importlib
os.environ["CREWE_AGENT_TOOLS"] = "0"
importlib.reload(agent)
seen = {"n": 0, "tools_seen": []}
def fc(messages, tools=None, **kw):
    seen["n"] += 1
    seen["tools_seen"].append(tools is not None)
    return '```json\n{"action":"finish","summary":"x"}\n```'
agent._chat = fc
res = agent.run_agent("x", max_steps=2, plan=False, backend=StubBackend(),
                      on_event=lambda e: None, coder={"url": "http://x/v1/chat/completions", "kind": "openai"})
check("kill switch: no tools offered even to an openai coder", seen["tools_seen"] == [False], str(seen["tools_seen"]))
os.environ["CREWE_AGENT_TOOLS"] = "1"
importlib.reload(agent)

print("--- condensation understands tool calls ---")
big = "x" * 5000
tcs = [{"id": "c", "type": "function", "function": {"name": "write",
        "arguments": json.dumps({"path": "big.py", "content": big})}}]
m = {"role": "assistant", "content": None, "tool_calls": tcs}
check("_msg_len counts tool-call arguments", agent._msg_len(m) > 5000, str(agent._msg_len(m)))
cc = agent._condense_tool_calls(tcs)
args = json.loads(cc[0]["function"]["arguments"])
check("a big file body inside a tool call is elided but the JSON stays valid and keeps the path",
      args["path"] == "big.py" and "elided" in args["content"] and len(cc[0]["function"]["arguments"]) < 500, str(args)[:80])
hist = [{"role": "system", "content": "S"}, {"role": "user", "content": "T"}, {"role": "user", "content": "W"}]
for i in range(8):
    hist.append({"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": "write",
         "arguments": json.dumps({"path": f"f{i}.py", "content": "y" * 4000})}}]})
    hist.append({"role": "tool", "tool_call_id": f"c{i}", "content": "wrote it — syntax OK"})
before = sum(agent._msg_len(m) for m in hist)
cond = {}
new, lvl, tot = agent._condense_history(hist, 3, cond, budget=8000)
check("an over-budget tool-mode history is condensed below budget",
      tot < before and tot <= 8000 * 1.5 and new > 0, f"{before}->{tot} new={new}")
check("condensed assistant turns keep valid tool-call JSON",
      all(json.loads(tc["function"]["arguments"]) for m in hist if m.get("tool_calls") for tc in m["tool_calls"]))

print("--- review fixes: verdict, gate, notes, ids, hijack, condense, fallback ---")

# A: a batched write + FAILING run + finish must not report verified=True
res, seen, events = run([[tool("write", path="x.py", content="print(1)\n"),
                          tool("run", cmd="python3 x.py"),
                          tool("finish", summary="d")]],
                        backend=StubBackend(exits={"python3 x.py": 1}))
check("A: [write, failing run, finish] in one step -> verified FALSE (not a false ✓)",
      res["done"] and res["verified"] is False and "exited 1" in (res.get("verify_note") or ""),
      str({k: res.get(k) for k in ("done", "verified", "verify_note")}))
res, seen, events = run([[tool("write", path="x.py", content="print(1)\n"),
                          tool("run", cmd="python3 x.py"),
                          tool("finish", summary="d")]], backend=StubBackend())
check("A: the same batch with a PASSING run -> verified True",
      res["done"] and res["verified"] is True, str(res.get("verify_note")))

# B: a batch of finishes must not burn the bounce budget or accept with unrun changes
res, seen, events = run([[tool("write", path="y.py", content="print(1)\n")],
                         [tool("finish", summary="a"), tool("finish", summary="b"),
                          tool("finish", summary="c")],
                         [tool("run", cmd="python3 y.py")],
                         [tool("finish", summary="done")]])
refusals = [e for e in events if e.get("t") == "warn" and "finish refused" in e.get("note", "")]
step2_tools = [m["content"] for m in seen["calls"][2] if m["role"] == "tool"]
check("B: three batched finishes cost ONE bounce, not three",
      len(refusals) == 1, f"{len(refusals)} refusals")
check("B: later finishes in the batch are told one-per-turn, not accepted",
      any("already tried to finish" in c for c in step2_tools) and res["steps"] == 4 and res["done"],
      str(step2_tools))

# C: a [finish, finish] batch with a note pending must defer (not swallow the note)
calls = {"n": 0}
def pn():
    calls["n"] += 1
    return ["add a docstring"] if calls["n"] == 2 else []
res, seen, events = run([[tool("write", path="a.py", content="print(1)\n")],
                         [tool("finish", summary="x"), tool("finish", summary="y")],
                         [tool("run", cmd="python3 a.py")],
                         [tool("finish", summary="done")]], pull_notes=pn)
notes_ev = [e for e in events if e.get("t") == "note"]
step2 = seen["calls"][2]
asst2 = [m for m in step2 if m["role"] == "assistant" and m.get("tool_calls")][-1]
tool_ids = {m["tool_call_id"] for m in step2 if m["role"] == "tool"}
check("C: the note is delivered exactly once and the finish is deferred",
      len(notes_ev) == 1 and "add a docstring" in notes_ev[0]["text"] and res["done"], str(notes_ev))
check("C: undelivered_notes is not set (the note WAS delivered)", res.get("undelivered_notes") is None)
check("C: both batched finish calls got a tool result -> valid re-sent history",
      {tc["id"] for tc in asst2["tool_calls"]} <= tool_ids and len(asst2["tool_calls"]) == 2, str(tool_ids))

# I + J: an accepted finish mid-batch ends cleanly; duplicate ids are de-duped
res, seen, events = run([[tool("run", cmd="echo hi")],
                         [tool("finish", summary="done"), tool("write", path="late.py", content="x")]])
check("I: a finish accepted mid-batch ends the run cleanly (orphan call stubbed)",
      res["done"] and res["steps"] == 2, str(res.get("steps")))
res, seen, events = run([[{"id": "dup", "name": "write", "arguments": json.dumps({"path": "a.py", "content": "1"})},
                          {"id": "dup", "name": "run", "arguments": json.dumps({"cmd": "ls"})}],
                         [tool("finish", summary="d")]])
step2 = seen["calls"][1]
asst = [m for m in step2 if m["role"] == "assistant" and m.get("tool_calls")][-1]
ids = [tc["id"] for tc in asst["tool_calls"]]
tool_ids = [m["tool_call_id"] for m in step2 if m["role"] == "tool"]
check("J: duplicate model tool ids are de-duplicated within the turn",
      len(set(ids)) == 2 and "dup" in ids and sorted(set(ids)) == sorted(set(tool_ids)), str(ids))

# K: the tool NAME wins over an `action` key smuggled into the arguments
b = StubBackend()
res, seen, events = run([[{"id": "h", "name": "write", "arguments": json.dumps(
                            {"action": "run", "cmd": "rm -rf /", "path": "a.py", "content": "safe"})}],
                         [tool("run", cmd="python3 a.py")],
                         [tool("finish", summary="d")]], backend=b)
check("K: a write call carrying {\"action\":\"run\"} still WRITES — the tool name is authoritative",
      b.files.get("a.py") == "safe" and "rm -rf /" not in b.ran, f"files={list(b.files)} ran={b.ran}")

# E: a huge `run` cmd is condensed in tool mode
cc = agent._condense_tool_calls([{"id": "c", "type": "function",
     "function": {"name": "run", "arguments": json.dumps({"cmd": "x" * 5000})}}])
ca = json.loads(cc[0]["function"]["arguments"])
check("E: a huge run cmd is elided (kept valid JSON)",
      "elided" in ca["cmd"] and len(cc[0]["function"]["arguments"]) < 500, str(ca)[:60])

# H: the 400->text fallback needs a REJECTION phrase, not a bare tool/function mention
import requests as _rq
def _http400(msg):
    r = _rq.Response(); r.status_code = 400
    r._content = ("{\"error\":{\"message\":\"" + msg + "\"}}").encode()
    raise _rq.exceptions.HTTPError(f"400 Bad Request for url: http://x — {msg}", response=r)
class Overflow:
    def __call__(self, messages, tools=None, **kw):
        if tools is not None:
            _http400("this request exceeds the maximum context length; your tools were included")
        return '```json\n{"action":"finish","summary":"x"}\n```'
agent._chat = Overflow()
ev2 = []
res = agent.run_agent("x", max_steps=4, plan=False, backend=StubBackend(), on_event=ev2.append,
                      coder={"url": "http://x/v1/chat/completions", "kind": "openai"})
check("H: a context-overflow 400 that merely mentions 'tools' is NOT misread as a tools rejection",
      not res.get("done") and res.get("error")
      and not any("rejected tool calling" in e.get("note", "") for e in ev2), str(res.get("error"))[:80])
class RealReject:
    def __init__(self): self.n = 0
    def __call__(self, messages, tools=None, **kw):
        self.n += 1
        if tools is not None and self.n == 1:
            _http400("tools are not supported by this model")
        return '```json\n{"action":"finish","summary":"text"}\n```'
agent._chat = RealReject()
ev3 = []
res = agent.run_agent("x", max_steps=4, plan=False, backend=StubBackend(), on_event=ev3.append,
                      coder={"url": "http://x/v1/chat/completions", "kind": "openai"})
check("H: a genuine 'tools not supported' 400 DOES drop to text and finishes",
      res.get("done") and any("rejected tool calling" in e.get("note", "") for e in ev3))

print(f"\n{len(ok)} passed, {len(bad)} failed")
if bad:
    print("FAILED:", bad)
sys.exit(1 if bad else 0)
