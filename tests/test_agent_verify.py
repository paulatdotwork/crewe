"""Agent loop: logic, looping and verification upgrades (offline).

Evidence these pin, from real runs in ~/llama_logs/agent:
  - a local-mode run spent steps 1-4 on `cd /workspace` because the system
    prompt hardcoded a sandbox it was not in         -> workspace briefing
  - a step was lost to a multi-line shell command inside JSON (literal
    newlines are invalid JSON)                        -> lenient JSON
  - 13 files written back-to-back with no check, then "complete" claimed for
    code that was never run                            -> syntax checks, finish gate,
                                                          an honest verified verdict
  - whole-file rewrites for small fixes                -> edit action
  - identical failing commands repeated, parse-fail streaks, multi-action
    replies, truncated writes                          -> loop notes + bounds

No critic model, no rounds: every check here is deterministic. Stubs the model
and the backend (no network, no Docker); the audit dir is redirected.
"""
import json
import os
import shutil
import sys
import tempfile
import time

_AUDIT_TMP = tempfile.mkdtemp(prefix="crewe-audit-test-")
os.environ["CREWE_AGENT_AUDIT_DIR"] = _AUDIT_TMP

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)
import agent

ok, bad = [], []


def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


class StubBackend:
    """Minimal workspace surface; per-command exit codes via `exits`."""

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


class ChangingBackend(StubBackend):
    """Same command, different output every time (a real clock, a counter)."""

    def run(self, cmd, timeout=None):
        self.ran.append(cmd)
        return self.exits.get(cmd, 0), f"ran:{cmd}:{len(self.ran)}", ""


def scripted(replies):
    seen = {"calls": [], "n": 0}

    def fake_chat(messages, **kw):
        seen["calls"].append([dict(m) for m in messages])
        seen["n"] += 1
        return replies[min(seen["n"] - 1, len(replies) - 1)]

    agent._chat = fake_chat
    return seen


def observations(msgs):
    return [m["content"] for m in msgs if m["role"] == "user"
            and m["content"].startswith("OBSERVATION")]


def run(replies, backend=None, max_steps=8, **kw):
    """Drive the loop; returns (result, seen, events)."""
    seen = scripted(replies)
    events = []
    res = agent.run_agent("do the task", max_steps=max_steps, plan=False,
                          backend=backend or StubBackend(),
                          on_event=events.append, **kw)
    return res, seen, events


FINISH = '```json\n{"action":"finish","summary":"done"}\n```'
RUN_PY = '```json\n{"action":"run","cmd":"python3 a.py"}\n```'
RUN_LS = '```json\n{"action":"run","cmd":"ls"}\n```'
WRITE_PY = "WRITE: a.py\n```\nprint('hi')\n```"
WRITE_BAD_PY = "WRITE: a.py\n```\ndef f(:\n    pass\n```"
WRITE_HTML = "WRITE: index.html\n```\n<html><body><p>hi</p></body></html>\n```"
READ = '```json\n{"action":"read","path":"a.py"}\n```'

# ---- 1. parsing ---------------------------------------------------------------
print("--- parsing ---")
a = agent._parse_action('```json\n{"action":"run","cmd":"python3 -c \\"\nimport re\nprint(1)\\""}\n```')
check("JSON with literal newlines inside a string parses (the real step-20 loss)",
      a is not None and a["action"] == "run" and "\nimport re\n" in a["cmd"], repr(a))
a = agent._parse_action("EDIT: app.py\n```find\n    return 1\n```\n```replace\n    return 2\n```")
check("raw EDIT form parses, indentation intact",
      a == {"action": "edit", "path": "app.py", "find": "    return 1",
            "replace": "    return 2"}, repr(a))
a = agent._parse_action("EDIT: app.py\n```find\nold\n```\n```replace\n```")
check("raw EDIT with an empty replace block = deletion",
      a is not None and a["replace"] == "" and a["find"] == "old", repr(a))
a = agent._parse_action("EDIT: doc.md\n```find\nx\n```\n```replace\nuse ```code``` here\n```")
check("replace block may contain fences (closes at the LAST fence)",
      a is not None and a["replace"] == "use ```code``` here", repr(a))
a = agent._parse_action('```json\n{"action":"edit","path":"a.py","find":"x","replace":"y"}\n```')
check("JSON edit form parses", a is not None and a["action"] == "edit" and a["find"] == "x")
a = agent._parse_action("WRITE: README.md\n```\nThe protocol:\nEDIT: foo\n```find\nx\n```\n```replace\ny\n```\n```")
check("a WRITE whose content documents EDIT is still a write (first header wins)",
      a is not None and a["action"] == "write" and a["path"] == "README.md"
      and "```replace" in a["content"], repr(a))
a = agent._parse_action("EDIT: a.py\n```find\nx\n```\n```replace\nWRITE: nope\n```")
check("an EDIT whose replace text mentions WRITE is still an edit",
      a is not None and a["action"] == "edit" and a["replace"] == "WRITE: nope", repr(a))
a = agent._parse_action("WRITE: big.py\n```\nprint(1)\nprint(2)")
check("unclosed WRITE fence is flagged truncated",
      a is not None and a["action"] == "write" and a.get("_truncated") is True)
a = agent._parse_action("WRITE: ok.py\n```\nprint(1)\n```")
check("closed WRITE is not flagged", a is not None and "_truncated" not in a)
check("old-style replies still parse", agent._parse_action(RUN_PY)["cmd"] == "python3 a.py")
check("two JSON actions counted", agent._count_actions(RUN_PY + "\n" + RUN_LS) == 2)
check("JSON then a raw WRITE counted as two",
      agent._count_actions(RUN_LS + "\n" + WRITE_PY) == 2)
check("one raw write counts once even if its content says 'action'",
      agent._count_actions('WRITE: a.json\n```\n{"action":"x"}\n{"action":"y"}\n```') == 1)
check("prose-only reply counts as one (never zero)", agent._count_actions("hello") == 1)

# ---- 2. syntax checks ---------------------------------------------------------
print("--- syntax checks ---")
r = agent._lint("a.py", "def f(:\n    pass\n")
check("python syntax error reported with its line", r is not None and r[0] is False and "line 1" in r[1], repr(r))
check("clean python -> OK", agent._lint("a.py", "x = 1\n") == (True, "syntax OK"))
check("bad json reported", agent._lint("c.json", "{bad}")[0] is False)
check("good json ok", agent._lint("c.json", '{"a": 1}') == (True, "valid JSON"))
check("JSON with comments (tsconfig-style) gets no verdict, not an error",
      agent._lint("tsconfig.json", '{\n  // strict\n  "a": 1\n}') is None)
check("unknown type -> None (no verdict), not a false OK",
      agent._lint("game.gd", "func _ready():\n\tpass") is None)
check("a crashing checker -> None", agent._lint("a.py", None) is None)
HAS_NODE = bool(shutil.which("node"))
if HAS_NODE:
    check("js syntax error reported", agent._lint("a.js", "function f( {\n")[0] is False)
    check("ESM syntax in a .js file is fine",
          agent._lint("a.js", "import x from 'y';\nexport const a = 1;\n") == (True, "syntax OK"))
    r = agent._lint("index.html",
                    "<html>\n<body>\n<script>\nlet a = 1;\nfunction f( {\n</script>\n</body></html>\n")
    check("inline <script> error names the PAGE line",
          r is not None and r[0] is False and "inline <script>" in r[1]
          and ("line 5" in r[1] or "line 6" in r[1]), repr(r))
    check("src= scripts and non-JS script types are skipped",
          agent._lint("i.html", '<script src="x.js"></script>'
                      '<script type="text/template">{{ not js }}</script>') == (True, "syntax OK"))
    check("module scripts are checked as ESM",
          agent._lint("m.html", '<script type="module">import {a} from "./a.js"; a();</script>')
          == (True, "syntax OK"))
    for name, body in (("App.js", "export default function App() {\n  return <div className='x'>hi</div>;\n}\n"),
                       ("App.js", "function App() {\n  return <div className='x'>hi</div>;\n}\nmodule.exports = App;\n"),
                       ("App.jsx", "const A = () => <p>x</p>;\n")):
        r = agent._lint(name, body)
        check(f"JSX ({name}, {'esm' if 'export' in body else 'cjs'}) is never called a syntax ERROR",
              r is None or r[0] is True, repr(r))
    check("templated inline scripts (Jinja/EJS) are skipped, never judged",
          agent._lint("templates/index.html",
                      "<script>\nvar data = {{ items|tojson }};\nfunction f( {\n</script>") == (True, "syntax OK"))
    check("plain JS typo in a page is still caught",
          agent._lint("index.html", "<script>\nfunction f( {\n</script>")[0] is False)
    saved = shutil.which
    shutil.which = lambda n, *a, **k: None if n == "node" else saved(n, *a, **k)
    try:
        check("node missing -> no verdict (None), never a false OK",
              agent._lint("a.js", "function f( {") is None)
    finally:
        shutil.which = saved
else:
    print("  (node not installed here — JS/HTML checks skipped)")
if shutil.which("bash"):
    check("shell syntax error reported", agent._lint("x.sh", "if [ 1 ]; then\necho hi\n")[0] is False)
    check("clean shell ok", agent._lint("x.sh", "echo hi\n") == (True, "syntax OK"))

# ---- 3. edit action -----------------------------------------------------------
print("--- edit action ---")
EDIT = "EDIT: app.py\n```find\n    return 1\n```\n```replace\n    return 2\n```"
sb = StubBackend(files={"app.py": "def f():\n    return 1\n"})
res, seen, ev = run([EDIT, '```json\n{"action":"run","cmd":"python3 app.py"}\n```', FINISH], backend=sb)
obs = observations(seen["calls"][-1])
check("edit applied exactly", sb.files["app.py"] == "def f():\n    return 2\n", repr(sb.files))
check("edit observation reports the change + syntax OK",
      "edited app.py" in obs[0] and "syntax OK" in obs[0], obs[0])
check("run completes after an edit", res["done"] is True)

sb = StubBackend(files={"app.py": "a\nb\n"})
res, seen, ev = run(["EDIT: app.py\n```find\nzzz\n```\n```replace\ny\n```", RUN_LS, FINISH], backend=sb)
obs = observations(seen["calls"][-1])
check("find text missing -> clear failure, file untouched",
      "not found" in obs[0] and "grep -n" in obs[0] and sb.files["app.py"] == "a\nb\n", obs[0])

sb = StubBackend(files={"app.py": "x = 1\nx = 1\n"})
res, seen, ev = run(["EDIT: app.py\n```find\nx = 1\n```\n```replace\nx = 2\n```", RUN_LS, FINISH], backend=sb)
obs = observations(seen["calls"][-1])
check("ambiguous find -> refused, never guesses",
      "occurs 2 times" in obs[0] and sb.files["app.py"] == "x = 1\nx = 1\n", obs[0])

sb = StubBackend(files={"app.py": "def f():\n    return 1   \n"})
res, seen, ev = run([EDIT, RUN_LS, FINISH], backend=sb)
check("find text that is a plain substring of the line applies, rest of the line kept",
      sb.files["app.py"] == "def f():\n    return 2   \n", repr(sb.files))
sb = StubBackend(files={"app.py": "def f():\n    return 1\n"})
res, seen, ev = run(["EDIT: app.py\n```find\n    return 1   \n```\n```replace\n    return 2\n```",
                     RUN_LS, FINISH], backend=sb)
check("trailing whitespace the model added to its find text is tolerated",
      sb.files["app.py"] == "def f():\n    return 2\n", repr(sb.files))

sb = StubBackend(files={"a.py": "keep\ndrop me\nkeep2\n"})
res, seen, ev = run(["EDIT: a.py\n```find\ndrop me\n```\n```replace\n```", RUN_LS, FINISH], backend=sb)
check("empty replace deletes the line (exact path keeps the newline)",
      "drop me" not in sb.files["a.py"] and "keep\n" in sb.files["a.py"], repr(sb.files["a.py"]))

sb = StubBackend()
res, seen, ev = run([EDIT, FINISH], backend=sb)
obs = observations(seen["calls"][-1])
check("edit of a missing file says so", "does not exist" in obs[0], obs[0])

sb = StubBackend(files={"a.py": "x = 1\n"})
res, seen, ev = run(["EDIT: a.py\n```find\nx = 1\n```\n```replace\nx = (\n```", RUN_LS, RUN_LS, FINISH], backend=sb)
obs = observations(seen["calls"][-1])
check("an edit that breaks the syntax is reported immediately",
      "SYNTAX ERROR in a.py" in obs[0], obs[0])

# ---- 4. the finish gate -------------------------------------------------------
print("--- finish gate ---")
res, seen, ev = run([WRITE_PY, FINISH, RUN_PY, FINISH], max_steps=8)
obs = observations(seen["calls"][-1])
check("finish refused after an unverified write", "NOT FINISHED" in obs[1], obs[1])
check("refusal names the file and a concrete command",
      "a.py" in obs[1] and "python3 a.py" in obs[1], obs[1])
check("refusal is a transcript warn event",
      any(e["t"] == "warn" and "finish refused" in e.get("note", "") for e in ev))
check("finish accepted once something ran", res["done"] is True and res["steps"] == 4)
check("verified, with the command as evidence",
      res["verified"] is True and "python3 a.py" in res["verify_note"], repr(res.get("verify_note")))
check("last run recorded", res["last_run"] == {"cmd": "python3 a.py", "exit": 0})
check("verdict reaches the transcript",
      any(e["t"] == "harness" and e.get("note", "").startswith("verification: ✓") for e in ev))
end = json.loads(open(res["audit"]).read().strip().splitlines()[-1])
check("audit end carries the verdict", end.get("verified") is True)

res, seen, ev = run([WRITE_PY, FINISH], max_steps=8)
check("the gate is bounded: two refusals, then finish lands",
      res["done"] is True and res["steps"] == 4, str(res["steps"]))
check("…but the run is marked unverified with the reason",
      res["verified"] is False and "nothing was run" in res["verify_note"], repr(res["verify_note"]))

res, seen, ev = run([RUN_LS, WRITE_PY, FINISH], max_steps=3)
check("no room left to verify -> finish accepted, not refused",
      res["done"] is True and res["steps"] == 3)
check("…and honestly unverified", res["verified"] is False)

res, seen, ev = run([WRITE_HTML, FINISH], max_steps=8)
check("static-only change needs no run: finish accepted at once", res["done"] and res["steps"] == 2)
check("static-only verdict names the syntax checks",
      res["verified"] is True and "static" in res["verify_note"], repr(res["verify_note"]))

res, seen, ev = run([WRITE_BAD_PY, RUN_PY, FINISH, WRITE_PY, RUN_PY, FINISH], max_steps=10)
obs = observations(seen["calls"][-1])
check("a syntax error is reported on the write", "SYNTAX ERROR in a.py" in obs[0], obs[0])
check("finish refused while the syntax error stands, even after a run",
      "NOT FINISHED" in obs[2] and "SYNTAX ERRORS" in obs[2], obs[2])
check("fixing the file clears the block", res["done"] is True and res["verified"] is True)

res, seen, ev = run([WRITE_PY, RUN_PY, FINISH], backend=StubBackend(exits={"python3 a.py": 1}))
check("a failing last run -> done but unverified, exit code named",
      res["done"] is True and res["verified"] is False and "exited 1" in res["verify_note"],
      repr(res.get("verify_note")))

res, seen, ev = run([RUN_LS, WRITE_PY, RUN_PY, WRITE_PY, FINISH], max_steps=8)
check("a change AFTER the last run re-arms the gate", res["steps"] == 7 or res["steps"] == 6)
check("…and the verdict says nothing ran after it", res["verified"] is False and "nothing was run" in res["verify_note"])

res, seen, ev = run([READ, FINISH], backend=StubBackend(files={"a.py": "x"}))
check("answer-only run (no changes): nothing to verify, verdict None",
      res["done"] is True and res["verified"] is None and "verify_note" in res)
check("no verdict event when there was nothing to verify",
      not any("verification:" in e.get("note", "") for e in ev))

# ---- 5. loop hygiene ----------------------------------------------------------
print("--- loop hygiene ---")
MAKE = '```json\n{"action":"run","cmd":"make"}\n```'
res, seen, ev = run([MAKE, MAKE, FINISH], backend=StubBackend(exits={"make": 2}))
obs = observations(seen["calls"][-1])
check("identical command + identical result -> loop note on the repeat",
      "identical to the failure at step 1" in obs[1] and "identical" not in obs[0], obs[1])
res, seen, ev = run([MAKE, MAKE, FINISH], backend=ChangingBackend(exits={"make": 2}))
obs = observations(seen["calls"][-1])
check("same command, different output -> no loop note", "identical" not in obs[1])

res, seen, ev = run([RUN_LS + "\n" + '```json\n{"action":"run","cmd":"pwd"}\n```', FINISH],
                    backend=(sb := StubBackend()))
obs = observations(seen["calls"][-1])
check("multi-action reply: only the first runs, and the model is told",
      "(your reply contained 2 actions" in obs[0]
      and obs[0].index("your reply contained") < obs[0].index("exit_code")
      and "ls" in sb.ran and "pwd" not in sb.ran, obs[0])

res, seen, ev = run(["WRITE: big.py\n```\nprint(1)\nprint(2)", RUN_PY, FINISH], backend=(sb := StubBackend()))
obs = observations(seen["calls"][-1])
check("truncated write lands AND is flagged",
      sb.files.get("big.py") == "print(1)\nprint(2)" and "without a closing" in obs[0]
      and "INCOMPLETE" in obs[0], obs[0])

res, seen, ev = run(["nope", "nope", "nope", FINISH], max_steps=8)
check("three unparseable replies in a row end the run",
      res["done"] is False and "unparseable" in res.get("error", "") and res["steps"] == 3, repr(res))
retry = [m["content"] for m in seen["calls"][-1] if m["role"] == "user" and "not a valid action" in m["content"]]
check("the second retry warns the run is about to stop", any("stops after 1 more" in r for r in retry), str(retry[-1:]))
res, seen, ev = run(["nope", "nope", FINISH], max_steps=8)
check("two failures then recovery continues normally", res["done"] is True and res["steps"] == 3)
res, seen, ev = run(["nope", RUN_LS, "nope", RUN_LS, "nope", FINISH], max_steps=10)
check("the streak counter resets on a good reply", res["done"] is True and "error" not in res)

# ---- 6. workspace briefing ----------------------------------------------------
print("--- workspace briefing ---")
check("system prompt no longer hardcodes /workspace", "/workspace" not in agent.SYSTEM)
check("system prompt teaches edit and the gate",
      "EDIT:" in agent.SYSTEM and "finish is REFUSED" in agent.SYSTEM)


class LocalWorkspace(StubBackend):
    workdir = "/home/u/crewe-agent/run1"


class Sandbox(StubBackend):
    workdir = "/workspace"


res, seen, ev = run([FINISH], backend=LocalWorkspace())
msgs = seen["calls"][0]
ws = [m["content"] for m in msgs if m["role"] == "user" and m["content"].startswith("WORKSPACE STATE")]
check("exactly one workspace message, right after the task",
      len(ws) == 1 and msgs[2]["content"].startswith("WORKSPACE STATE"))
check("names the real working directory", "/home/u/crewe-agent/run1" in ws[0], ws[0][:200])
check("local target is called unsandboxed", "NOT sandboxed" in ws[0])
check("tool probe included", "Tools available" in ws[0])
check("fresh workspace is called empty", "workspace is empty" in ws[0])
res, seen, ev = run([FINISH], backend=Sandbox())
ws = [m["content"] for m in seen["calls"][0] if m["role"] == "user" and m["content"].startswith("WORKSPACE STATE")][0]
check("sandbox target: container + network scope stated",
      "/workspace" in ws and "Docker" in ws and "package mirrors" in ws, ws[:200])


class Restorable(StubBackend):
    workdir = "/workspace"

    def restore(self, seed_dir):
        self.files.update({"index.html": "<p>x</p>", "main.py": "print(1)\n"})
        return True


with tempfile.TemporaryDirectory() as seed:
    res, seen, ev = run([FINISH], backend=Restorable(), seed_dir=seed)
ws = [m["content"] for m in seen["calls"][0] if m["role"] == "user" and m["content"].startswith("WORKSPACE STATE")]
check("restored workspace lists its files (no step spent on ls)",
      len(ws) == 1 and "Files present:" in ws[0] and "main.py" in ws[0] and "smallest edit" in ws[0], ws[0][-300:])
check("…and is not called empty", "workspace is empty" not in ws[0])


class Dead(StubBackend):
    def run(self, cmd, timeout=None):
        raise RuntimeError("no shell")


res, seen, ev = run([FINISH], backend=Dead())
check("a failing tool probe never kills the run", res["done"] is True)

# ---- 7. run time cap ----------------------------------------------------------
print("--- run time cap ---")
saved_cap = agent.RUN_MAX_SECS
agent.RUN_MAX_SECS = 1
events = []
def slow_chat(messages, **kw):
    time.sleep(0.6)
    return RUN_LS
agent._chat = slow_chat
res = agent.run_agent("t", max_steps=10, plan=False, backend=StubBackend(), on_event=events.append)
agent.RUN_MAX_SECS = saved_cap
check("run time cap stops the loop", res.get("timed_out") is True and res["steps"] <= 4, repr(res))
check("…as an exhausted (best-effort) run, not an error",
      res.get("exhausted") is True and "error" not in res and res["done"] is False)
check("the cap is said out loud once, not as a step-budget message",
      sum("time cap" in e.get("note", "") for e in events) == 1
      and not any("exhausted before finish" in e.get("note", "") for e in events))
check("default cap is hours, not minutes, and env-tunable",
      saved_cap >= 3600 and "CREWE_AGENT_RUN_MAX" in open(agent.__file__).read())


# ---- 9. review round: edit safety ----------------------------------------------
print("--- review round: edit safety ---")


class Clipping(StubBackend):
    """read_file honours max_bytes like the real backends (they clip)."""

    def read_file(self, path, max_bytes=200_000):
        c = self.files.get(path)
        return None if c is None else c[:max_bytes]


BIG = "LAST = 1\n" + ("x = 1\n" * 400_000)            # ~2.4 MB
sb = Clipping(files={"big.py": BIG})
res, seen, ev = run(["EDIT: big.py\n```find\nLAST = 1\n```\n```replace\nLAST = 2\n```", RUN_LS, FINISH], backend=sb)
obs = observations(seen["calls"][-1])
check("edit of a file past the cap is REFUSED, never truncated",
      "too large" in obs[0] and sb.files["big.py"] == BIG, obs[0][:120])
sb = Clipping(files={"mid.py": "LAST = 1\n" + ("x = 1\n" * 50_000)})   # ~300 KB: over read_file's clip, under the edit cap
res, seen, ev = run(["EDIT: mid.py\n```find\nLAST = 1\n```\n```replace\nLAST = 2\n```", RUN_LS, FINISH], backend=sb)
check("a 300 KB file (over read_file's default clip) edits intact",
      sb.files["mid.py"].startswith("LAST = 2\n") and sb.files["mid.py"].count("x = 1\n") == 50_000)
sb = StubBackend(files={"l1.py": "name = 'Jos�'\nx = 1\n"})
res, seen, ev = run(["EDIT: l1.py\n```find\nx = 1\n```\n```replace\nx = 2\n```", RUN_LS, FINISH], backend=sb)
obs = observations(seen["calls"][-1])
check("a file that did not decode cleanly is refused (a round-trip would damage it)",
      "not valid UTF-8" in obs[0] and "x = 1" in sb.files["l1.py"], obs[0][:120])
res, seen, ev = run(["WRITE: t.py\n```\na = 1\nb = 2\n```", RUN_PY, FINISH])
obs = observations(seen["calls"][-1])
check("line counts are real counts", "(2 lines)" in obs[0], obs[0][:80])

# ---- 10. review round: stale syntax verdicts ----------------------------------
print("--- review round: stale syntax verdicts ---")


class Shell(StubBackend):
    """a run can change files, like a real shell (sed -i, cat >, rm)"""

    def run(self, cmd, timeout=None):
        self.ran.append(cmd)
        if cmd.startswith("fix "):
            self.files[cmd[4:]] = "print(42)\n"
        if cmd.startswith("rm "):
            self.files.pop(cmd[3:], None)
        return 0, f"ran:{cmd}", ""


FIX = '```json\n{"action":"run","cmd":"fix a.py"}\n```'
RM = '```json\n{"action":"run","cmd":"rm a.py"}\n```'
res, seen, ev = run([WRITE_BAD_PY, FIX, FINISH], backend=Shell())
check("a syntax error fixed by a shell command no longer blocks finish",
      res["done"] and res["steps"] == 3 and res["verified"] is True, repr((res["steps"], res.get("verify_note"))))
res, seen, ev = run([WRITE_BAD_PY, RM, FINISH], backend=Shell())
check("a removed file is not held against the model",
      res["done"] and res["steps"] == 3 and res["verified"] is True, repr((res["steps"], res.get("verify_note"))))
res, seen, ev = run([WRITE_BAD_PY, "WRITE: ./a.py\n```\nprint(1)\n```", RUN_PY, FINISH])
check("./a.py and a.py are the same file to the gate",
      res["done"] and res["steps"] == 4 and res["verified"] is True, repr((res["steps"], res.get("verify_note"))))
res, seen, ev = run([WRITE_BAD_PY, RUN_PY, FINISH, FINISH, FINISH], max_steps=10)
check("a syntax error that is still there still blocks (the re-check is honest)",
      res["verified"] is False and "syntax errors" in res["verify_note"], repr(res.get("verify_note")))

# ---- 11. review round: reply splitting ----------------------------------------
print("--- review round: reply splitting ---")
sb = StubBackend(files={"a.py": "x = 1\n"})
res, seen, ev = run(["EDIT: a.py\n```find\nx = 1\n```\n```replace\nx = 2\n```\n\n" + RUN_PY, FINISH], backend=sb)
obs = observations(seen["calls"][-1])
check("an EDIT followed by a JSON action: the file gets ONLY the edit", sb.files["a.py"] == "x = 2\n", repr(sb.files["a.py"]))
check("…and the model is told 2 actions were sent", "contained 2 actions" in obs[0], obs[0][:100])
sb = StubBackend()
res, seen, ev = run(["WRITE: a.py\n```\nprint(1)\n```\nWRITE: b.py\n```\nprint(2)\n```", RUN_PY, FINISH], backend=sb)
obs = observations(seen["calls"][-1])
check("two raw writes: the first file holds only its own content",
      sb.files.get("a.py") == "print(1)" and "b.py" not in sb.files, repr(sb.files))
check("…counted as 2", "contained 2 actions" in obs[0], obs[0][:100])
readme = "WRITE: README.md\n```\nUse it:\n```json\n{\"action\":\"run\",\"cmd\":\"ls\"}\n```\nThe end.\n```"
a = agent._parse_action(readme)
check("a README with a ```json example inside its open fence stays whole",
      a["action"] == "write" and "The end." in a["content"] and '"cmd"' in a["content"], repr(a))
check("…and counts as one action", agent._count_actions(readme) == 1)
doc = "WRITE: PROTOCOL.md\n```\nTwo forms:\nWRITE: x\n```\ncontent\n```\nEDIT: y\n```find\na\n```\n```replace\nb\n```\n```"
a = agent._parse_action(doc)
check("a doc whose content shows WRITE/EDIT examples is one write",
      a["action"] == "write" and a["path"] == "PROTOCOL.md" and "EDIT: y" in a["content"], repr(a))
check("…counted as one", agent._count_actions(doc) == 1)

# ---- 12. review round: loop-nag precision -------------------------------------
print("--- review round: loop-nag precision ---")
TEST = '```json\n{"action":"run","cmd":"pytest -q"}\n```'
res, seen, ev = run([WRITE_PY, TEST, "EDIT: a.py\n```find\nprint('hi')\n```\n```replace\nprint('ho')\n```", TEST, FINISH])
obs = observations(seen["calls"][-1])
check("a passing re-run after an edit gets no loop nag", "identical" not in obs[3], obs[3][:120])
res, seen, ev = run([MAKE, MAKE, FINISH])                                   # exit 0 both times
obs = observations(seen["calls"][-1])
check("a repeated PASSING command gets no nag", "identical" not in obs[1])
res, seen, ev = run([MAKE, WRITE_PY, MAKE, FINISH], backend=StubBackend(exits={"make": 2}))
obs = observations(seen["calls"][-1])
check("a failing command repeated AFTER a change gets no nag", "identical" not in obs[2])

# ---- 13. review round: what obliges a run ------------------------------------
print("--- review round: what obliges a run ---")
res, seen, ev = run([WRITE_PY, RUN_PY, "WRITE: Makefile\n```\nall:\n\techo hi\n```",
                     "WRITE: .gitignore\n```\n*.pyc\n```", FINISH])
check("config/docs written after the tests do not force another run",
      res["done"] and res["steps"] == 5 and res["verified"] is True, repr((res["steps"], res.get("verify_note"))))
res, seen, ev = run([WRITE_PY, RUN_PY, "WRITE: game.gd\n```\nfunc _ready():\n\tpass\n```", FINISH])
check("a source file of a runnable kind (.gd) written after the run still needs one",
      res["steps"] > 4 and res["verified"] is False)

# ---- 14. review round: lint precision ----------------------------------------
print("--- review round: lint precision ---")
check("BOM is not a python syntax error", agent._lint("bom.py", "﻿print(1)\n") == (True, "syntax OK"))
check("BOM is not a json error", agent._lint("b.json", "﻿{\"a\": 1}") == (True, "valid JSON"))
check("non-string content -> no verdict", agent._lint("a.py", None) is None)
if HAS_NODE:
    check("a commented-out broken script is not judged",
          agent._lint("i.html", "<!--\n<script>\nfunction f( {\n</script>\n-->\n<script>\nlet a = 1;\n</script>")
          == (True, "syntax OK"))
    r = agent._lint("i.html", "<!-- note -->\n<script>\nlet a = 1;\n</script>\n<script>\nfunction f( {\n</script>")
    check("page line numbers hold after a comment", r is not None and r[0] is False
          and ("line 6" in r[1] or "line 7" in r[1]), repr(r))
    check("a page with hundreds of scripts gets no verdict instead of minutes of node",
          agent._lint("many.html", "<script>let a=1;</script>\n" * 60) is None)
if shutil.which("bash"):
    check("extglob patterns are not a shell syntax error",
          agent._lint("x.sh", "shopt -s extglob\ncase $1 in @(a|b)) echo ab;; esac\n") == (True, "syntax OK"))

# ---- 15. review round 2 ---------------------------------------------------------
print("--- review round 2 ---")
new, why = agent._apply_edit("x = 1  # TODO\ny = 2\n", "  # TODO", "")
check("empty replace on a PARTIAL line keeps the rest of the line",
      new == "x = 1\ny = 2\n", repr(new))
new, why = agent._apply_edit("keep\ndrop me\nkeep2\n", "drop me", "")
check("empty replace on a whole line deletes the line", new == "keep\nkeep2\n", repr(new))
new, why = agent._apply_edit("a\nb\nc\n", "a\nb", "")
check("empty replace on whole leading lines deletes them", new == "c\n", repr(new))


class ClippingShell(Clipping):
    """read_file clips like the real backends AND a run can rewrite files."""

    def run(self, cmd, timeout=None):
        self.ran.append(cmd)
        if cmd.startswith("fix "):
            self.files[cmd[4:]] = ("y = 2\n" * 50_000) + "print(1)\n"   # ~300 KB, clean
        return 0, f"ran:{cmd}", ""


sb = ClippingShell()
res, seen, ev = run(["WRITE: big.py\n```\ndef f(:\n" + "x = 1\n" * 50_000 + "```",
                     '```json\n{"action":"run","cmd":"fix big.py"}\n```', FINISH], backend=sb)
check("a fixed file over the 200 KB read clip is re-judged whole, not truncated",
      res["done"] and res["steps"] == 3 and res["verified"] is True, repr((res["steps"], res.get("verify_note"))))
if HAS_NODE:
    check("'<!--' inside a JS string with a real comment later is not blanked across the script",
          agent._lint("i.html", '<script>const s = "<!--"; ok();</script>\n<p>x</p>\n<!-- note -->\n')
          == (True, "syntax OK"))
    check("a commented-out script is still skipped",
          agent._lint("i.html", "<!--\n<script>function f( {</script>\n-->\n") == (True, "syntax OK"))

# ---- 8. router wiring ---------------------------------------------------------
print("--- router wiring ---")
import router
tmp = tempfile.mkdtemp()
router.USERS_FILE = os.path.join(tmp, "u.json")
router.USERDATA_ROOT = os.path.join(tmp, "d")
router.app.config["SESSION_COOKIE_SECURE"] = False
router.AGENT_BROWSER_CHECK = True
router.create_user("admin@x.com", "correct-horse-battery", is_admin=True)
adm = router.app.test_client()
adm.post("/login", data={"email": "admin@x.com", "password": "correct-horse-battery"})


def wait_done(jid):
    for _ in range(100):
        p = adm.get(f"/agent/progress/{jid}?since=0").get_json()
        if p.get("done"):
            return p
        time.sleep(0.05)
    return None


def fake_run_factory(files, **extra):
    def fake_run(task, **kw):
        os.makedirs(kw["snapshot_dir"], exist_ok=True)
        for name, body in files.items():
            mode = "wb" if isinstance(body, bytes) else "w"
            with open(os.path.join(kw["snapshot_dir"], name), mode) as f:
                f.write(body)
        res = {"done": True, "summary": "ok", "files": list(files), "steps": 2,
               "snapshot_dir": kw["snapshot_dir"], "verified": True,
               "verify_note": "only static files changed"}
        res.update(extra)
        return res
    return fake_run


def start(files, **extra):
    agent.run_agent = fake_run_factory(files, **extra)
    r = adm.post("/agent/run", json={"task": "page", "plan": False})
    assert r.status_code == 200, r.get_json()
    return wait_done(r.get_json()["job_id"])


calls = []
router._browser_check = lambda files, **kw: (calls.append(sorted(files)) or
                                       (["TypeError: x is not a function"], "/tmp/shot.png"))
p = start({"index.html": "<html></html>", "app.js": "x()"})
check("html build triggers the browser check with the snapshot files",
      calls and calls[-1] == ["app.js", "index.html"], repr(calls))
check("browser errors demote the verdict", p["verified"] is False and "TypeError" in p["verify_note"], repr(p.get("verify_note")))
check("…and reach the transcript as a warn",
      any(e["t"] == "warn" and "browser check" in e.get("note", "") for e in p["events"]))

router._browser_check = lambda files, **kw: ([], "/tmp/shot.png")
p = start({"index.html": "<html></html>"})
check("clean browser check confirms the verdict",
      p["verified"] is True and "no runtime errors" in p["verify_note"], repr(p.get("verify_note")))

router._browser_check = lambda files, **kw: ([], None)
p = start({"index.html": "<html></html>"})
check("an UNAVAILABLE browser check never claims clean",
      p["verified"] is True and p["verify_note"] == "only static files changed"
      and any("unavailable" in e.get("note", "") for e in p["events"]), repr(p.get("verify_note")))

router._browser_check = lambda files, **kw: ([], "/tmp/shot.png")
p = start({"index.html": "<html></html>"}, verified=False, verify_note="nothing was run after the last change to a.py")
check("a clean page does not overturn an agent-level failure",
      p["verified"] is False and "nothing was run" in p["verify_note"])

calls.clear()
router._browser_check = lambda files, **kw: (calls.append(1) or ([], None))
p = start({"a.py": "print(1)"})
check("no html -> no browser check", not calls)

router.AGENT_BROWSER_CHECK = False
p = start({"index.html": "<html></html>"})
check("knob off -> no browser check", not calls and p["verified"] is True)
router.AGENT_BROWSER_CHECK = True

def boom(files, **kw):
    raise RuntimeError("gecko exploded")
router._browser_check = boom
p = start({"index.html": "<html></html>"})
check("a crashing browser check is reported, never fatal",
      p["done"] and any("browser check skipped" in e.get("note", "") for e in p["events"]))

p = start({"a.py": "x"}, done=False, exhausted=True, timed_out=True)
check("timed_out reaches the progress JSON", p["timed_out"] is True and p["exhausted"] is True)

# snapshot reader hygiene
d = tempfile.mkdtemp()
open(os.path.join(d, "index.html"), "w").write("<p>hi</p>")
open(os.path.join(d, "blob.bin"), "wb").write(b"\x00\x01\x02")
os.symlink("/etc/hostname", os.path.join(d, "link.html"))
os.mkfifo(os.path.join(d, "stall.html"))
fs = router._snapshot_text_files(d)
check("snapshot reader: text in, binary out", "index.html" in fs and "blob.bin" not in fs)
check("snapshot reader never follows symlinks (exfil guard)", "link.html" not in fs)
check("snapshot reader never opens FIFOs (hang guard)", "stall.html" not in fs)


# review round: the post-run check must never judge what is not this run's work
calls.clear()
router._browser_check = lambda files, **kw: (calls.append(1) or (["TypeError: x"], "/tmp/shot.png"))
p = start({"index.html": "<html></html>"}, verified=None, verify_note=None)
check("answer-only run (nothing changed) is not judged by the old page", not calls and p["verified"] is None)
calls.clear()
p = start({"index.html": "<html></html>", "sprite.png": b"\x89PNG\x00\x00\x00"})
check("a page whose assets the check cannot serve is not judged",
      not calls and p["verified"] is True and any("cannot serve" in e.get("note", "") for e in p["events"]))
calls.clear()
p = start({"index.html": '<html><script src="https://cdn.example.com/lib.js"></script></html>'})
check("a page with external URLs is skipped (the check runs offline)",
      not calls and any("external URLs" in e.get("note", "") for e in p["events"]))
seen_kw = {}
router._browser_check = lambda files, **kw: (seen_kw.update(kw) or ([], "/tmp/shot.png"))
p = start({"index.html": "<html></html>"})
check("the post-run check runs the browser OFFLINE", seen_kw.get("offline") is True, repr(seen_kw))
router._browser_check = lambda files, **kw: (["BLANK SCREEN: the page renders as a single flat color"], "/tmp/shot.png")
p = start({"index.html": "<html></html>"})
check("the BLANK SCREEN heuristic never demotes the verdict",
      p["verified"] is True and any("rendered blank" in e.get("note", "") for e in p["events"]))
calls.clear()
router._browser_check = lambda files, **kw: (calls.append(1) or ([], "/tmp/shot.png"))
p = start({"index.html": '<html><script src="//cdn.jsdelivr.net/npm/chart.js"></script></html>'})
check("a scheme-relative //cdn reference also skips the offline check",
      not calls and any("external URLs" in e.get("note", "") for e in p["events"]))
calls.clear()
p = start({"index.html": "<html><p>hi</p></html>", "README.md": "see https://github.com/x/y"})
check("a README link does not stop the page from being checked", calls == [1], repr(calls))
skipped = []
router._snapshot_text_files(d, skipped=skipped)
check("snapshot reader names what it could not serve", skipped == ["blob.bin"], repr(skipped))

page = router.AGENT_PAGE
check("UI shows the verdict", "✓ verified" in page and "unverified" in page)
check("UI names the time cap", "ran out of time" in page)

print(f"\n{len(ok)} passed, {len(bad)} failed")
raise SystemExit(1 if bad else 0)
