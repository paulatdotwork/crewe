"""Prompt helper: rough idea -> a sharper task.

The helper is a SUGGESTION path — it must never start a run, never hold the
agent slot, and never hand back something the operator would paste blind. So
what is worth testing is:
  1. parsing survives a small model's wandering (markdown labels, code fences,
     <think> leakage, a preamble) and still returns the usable draft;
  2. the route is admin-gated like the rest of /agent, validates its inputs,
     and turns a dead/uselessly-empty coder into a clean 502 instead of an
     exception page;
  3. it stays a suggestion: no job, no project, no lock taken.

No network and no live model — agent._chat is stubbed. Throwaway user stores,
house style (USERS_FILE/USERDATA_ROOT redirected before anything touches them).
"""
import os
import sys
import tempfile

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

router.create_user("admin@x.com", "correct-horse-battery", is_admin=True)
router.create_user("guest@x.com", "correct-horse-battery", is_admin=False)
adm, gst = router.app.test_client(), router.app.test_client()
adm.post("/login", data={"email": "admin@x.com", "password": "correct-horse-battery"})
gst.post("/login", data={"email": "guest@x.com", "password": "correct-horse-battery"})

SNAKE = "Build a single self-contained index.html snake game playable with the arrow keys."
# the prompt asks for the bare task, but a small model will label it anyway
CLEAN = f"TASK:\n{SNAKE}\n"

# ---- _parse_prompt_help ------------------------------------------------------
print("--- _parse_prompt_help ---")
s = router._parse_prompt_help(CLEAN)
check("labelled reply -> the task, label stripped", s == SNAKE, repr(s))
check("returns a plain string, no leftover halves", isinstance(s, str))
check("markdown-bold label",
      router._parse_prompt_help("**TASK:** do the thing") == "do the thing")
check("heading label",
      router._parse_prompt_help("## Task\ndo the thing") == "do the thing")
check("code fence stripped",
      router._parse_prompt_help("```\nTASK:\nwrapped in a fence\n```") == "wrapped in a fence")
check("<think> stripped",
      router._parse_prompt_help("<think>hmm, snake?</think>\nTASK:\nno leaked reasoning")
      == "no leaked reasoning")
check("a preamble before the label is dropped",
      router._parse_prompt_help("Sure, here you go:\n\nTASK:\nthe real draft") == "the real draft")
check("unlabelled reply is the task (the form the prompt asks for)",
      router._parse_prompt_help("a bare rewrite with no labels at all")
      == "a bare rewrite with no labels at all")
check("surrounding quotes stripped",
      router._parse_prompt_help('"quoted draft"') == "quoted draft")
check("empty reply -> None", router._parse_prompt_help("") is None)
check("label with nothing after it -> None", router._parse_prompt_help("TASK:\n") is None)
check("capped at 4000 chars",
      len(router._parse_prompt_help("TASK:\n" + "x" * 9000)) == 4000)

# ---- the route ---------------------------------------------------------------
print("--- /agent/prompt-help ---")
CALLS = []
def fake_chat(messages, **kw):
    CALLS.append({"messages": messages, "kw": kw})
    return CLEAN
_orig_chat = agent._chat
agent._chat = fake_chat

check("non-admin BLOCKED (403)",
      gst.post("/agent/prompt-help", json={"task": "x"}).status_code == 403)

r = adm.post("/agent/prompt-help", json={"task": "snake game"})
j = r.get_json()
check("happy path 200 + the task, and nothing else",
      r.status_code == 200 and j == {"task": SNAKE}, f"{r.status_code} {j}")
check("one model call, system + user only",
      len(CALLS) == 1 and [m["role"] for m in CALLS[0]["messages"]] == ["system", "user"])
check("the operator's idea reaches the model",
      "snake game" in CALLS[0]["messages"][1]["content"])
check("short silence timeout, room for a full brief",
      CALLS[0]["kw"].get("timeout") == (10, 180) and CALLS[0]["kw"].get("max_tokens") == 1200,
      repr(CALLS[0]["kw"]))

# the model cannot see the step select, so the route must state the real
# budget — briefing 40 steps like a one-shot toy is what produced the
# "RPG becomes one combat screen" failure
CALLS.clear()
adm.post("/agent/prompt-help", json={"task": "an RPG", "steps": 40})
msg = CALLS[0]["messages"][1]["content"]
check("the real budget reaches the model", "40 agent steps" in msg, msg[:120])
check("follow-ups are named as part of the budget", "follow-up" in msg, msg[:120])
check("no rounds language: one build, then follow-ups", "round" not in msg.lower(), msg[:160])
CALLS.clear()
adm.post("/agent/prompt-help", json={"task": "an RPG"})
check("budget defaults are sane with no select sent",
      f"{router.AGENT_MAX_STEPS} agent steps" in CALLS[0]["messages"][1]["content"],
      CALLS[0]["messages"][1]["content"][:120])
CALLS.clear()
r = adm.post("/agent/prompt-help", json={"task": "an RPG", "steps": "lots", "rounds": 99})
msg = CALLS[0]["messages"][1]["content"]
check("junk budget values clamped, never raised",
      r.status_code == 200 and f"{router.AGENT_MAX_STEPS} agent steps" in msg, msg[:120])
check("a stale 'rounds' key from an old page is ignored, not echoed",
      "99" not in msg and "round" not in msg.lower(), msg[:160])

# the prompt itself is the whole feature — these are the lines that stop it
# from rewriting an ambitious idea into a smaller, different one
print("--- PROMPT_HELPER_SYSTEM invariants ---")
sysmsg = CALLS[0]["messages"][0]["content"]
check("brief is told the build is not one shot: follow-ups continue it",
      "NOT one shot" in sysmsg and "follow-up" in sysmsg.lower())
check("scale preservation is stated", "smaller, different artifact" in sysmsg)
check("named existing works keep their structure", "existing work" in sysmsg)
check("does not default every build to a web page",
      "Do NOT default to a web page" in sysmsg and "project structure" in sysmsg)
check("an explicitly named platform wins",
      "language, engine, or platform" in sysmsg)
check("drafting warns against guessed facts about a named work",
      "guessed \"fact\"" in sysmsg)
check("drafting is told answers add, never subtract",
      "ADD constraints" in sysmsg and "not a scope cut" in sysmsg)
check("asks for the bare task — no second half to forget",
      "nothing else" in sysmsg and "BAR" not in sysmsg)
check("no MVP language leaked in from the planner",
      not any(w in sysmsg.lower() for w in
              ("minimal", "smallest version", "mvp", "bounded", "achievable")),
      sysmsg)
check("no gauntlet residue in the brief",
      not any(w in sysmsg.lower() for w in ("gauntlet", "critic", "quality bar", "round")),
      sysmsg)

check("no job was created", not router.AGENT_JOBS)
check("the agent slot is still free", router.AGENT_ACTIVE.acquire(blocking=False))
router.AGENT_ACTIVE.release()

check("empty task -> 400", adm.post("/agent/prompt-help", json={"task": "   "}).status_code == 400)
check("missing task -> 400", adm.post("/agent/prompt-help", json={}).status_code == 400)
r = adm.post("/agent/prompt-help", json={"task": "x", "coder": "no-such-backend"})
check("unknown coder -> 400", r.status_code == 400)
check("unknown coder does not call the model", len(CALLS) == 1)

# 'default' must resolve to the SAME coder the build itself uses. It used to be
# resolved only inside the build worker, so planning and this helper silently
# ran on agent.py's hardcoded 127.0.0.1:8080 while builds used the configured
# backend — one word, two models, no way to tell from the UI.
CALLS.clear()
_saved_dflt = router._agent_default_coder
router._agent_default_coder = lambda: {"url": "http://10.0.0.7:8080/v1/chat/completions",
                                       "key": "sk-x", "model": "m"}
try:
    for stage in ("ask", "draft"):
        CALLS.clear()
        adm.post("/agent/prompt-help", json={"task": "x", "stage": stage})
        check(f"{stage} stage uses the configured default coder, not a fallback url",
              (CALLS[0]["kw"].get("coder") or {}).get("url")
              == "http://10.0.0.7:8080/v1/chat/completions", repr(CALLS[0]["kw"].get("coder")))
finally:
    router._agent_default_coder = _saved_dflt

CALLS.clear()
adm.post("/agent/prompt-help", json={"task": "y" * 9000})
idea_sent = CALLS[0]["messages"][1]["content"].split("Rough idea: ", 1)[1]
check("oversized idea truncated before the model call", len(idea_sent) == 4000, len(idea_sent))

def dead_chat(messages, **kw):
    raise RuntimeError("connection refused")
agent._chat = dead_chat
r = adm.post("/agent/prompt-help", json={"task": "x"})
check("dead coder -> 502, not a traceback", r.status_code == 502 and "error" in r.get_json())

agent._chat = lambda messages, **kw: "   \n  "
r = adm.post("/agent/prompt-help", json={"task": "x"})
check("empty model reply -> 502", r.status_code == 502)

agent._chat = lambda messages, **kw: "TASK:\n"
r = adm.post("/agent/prompt-help", json={"task": "x"})
check("label with no draft behind it -> 502", r.status_code == 502)

# ---- the ask stage -----------------------------------------------------------
# Asking is a convenience wrapped around the same draft call: it may return
# nothing, it may fail outright, and neither is allowed to dead-end the flow.
print("--- _parse_prompt_questions ---")
QS = """Q: What should this run on?
- Godot, standalone
- Python + pygame
- a web page

Q: What does "modernized" mean here?
- color and higher resolution only
- quality-of-life too (autosave, fast travel)
"""
qs = router._parse_prompt_questions(QS)
check("two questions with their options", len(qs) == 2 and len(qs[0]["options"]) == 3
      and qs[0]["q"].startswith("What should this run on"), repr(qs))
check("options keep their order (first = the default)", qs[0]["options"][0] == "Godot, standalone")

qs = router._parse_prompt_questions("**Q1:** Which platform?\n* Godot\n* pygame")
check("bold/numbered label + '*' bullets", len(qs) == 1 and qs[0]["options"] == ["Godot", "pygame"], repr(qs))

qs = router._parse_prompt_questions("Here are my thoughts on your idea.\nIt sounds fun.")
check("prose with no questions -> nothing", qs == [], repr(qs))

qs = router._parse_prompt_questions("Q: Which platform?\n- Godot\nQ: A statement, not a question.\n- x")
check("a 'question' without a '?' is dropped", len(qs) == 1, repr(qs))

qs = router._parse_prompt_questions("\n".join(f"Q: Question {i}?\n- a\n- b" for i in range(9)))
check("questions capped at 4", len(qs) == 4, len(qs))

qs = router._parse_prompt_questions("Q: Which?\n" + "\n".join(f"- opt{i}" for i in range(12)))
check("options capped at 5", len(qs[0]["options"]) == 5, len(qs[0]["options"]))

qs = router._parse_prompt_questions("<think>what to ask…</think>\nQ: Which platform?\n- Godot")
check("<think> stripped before parsing", len(qs) == 1 and qs[0]["q"] == "Which platform?", repr(qs))

qs = router._parse_prompt_questions("Q: " + "z" * 900 + "?\n- " + "y" * 900)
check("question and option text capped",
      len(qs[0]["q"]) == 300 and len(qs[0]["options"][0]) == 140,
      f'{len(qs[0]["q"])}/{len(qs[0]["options"][0])}')

print("--- _clean_answers ---")
a = router._clean_answers([{"q": " Which  platform? ", "a": "\nGodot\n"}])
check("answers normalised", a == [{"q": "Which platform?", "a": "Godot"}], repr(a))
check("blank answers dropped, not sent as empty",
      router._clean_answers([{"q": "Which platform?", "a": "   "}]) == [])
check("junk shapes ignored", router._clean_answers(["nope", 7, None, {"a": "no q"}]) == [])
check("non-list ignored", router._clean_answers({"q": "x"}) == [] and router._clean_answers(None) == [])
check("answer list capped", len(router._clean_answers([{"q": f"q{i}?", "a": "a"} for i in range(20)])) == 6)
a = router._clean_answers([{"q": "x?" * 400, "a": "y" * 900}])
check("answer text capped", len(a[0]["q"]) == 300 and len(a[0]["a"]) == 300)

print("--- /agent/prompt-help stages ---")
CALLS.clear()
agent._chat = fake_chat_qs = lambda messages, **kw: (CALLS.append({"messages": messages, "kw": kw}) or QS)
r = adm.post("/agent/prompt-help", json={"task": "an RPG", "stage": "ask"})
j = r.get_json()
check("ask stage returns questions", r.status_code == 200 and len(j["questions"]) == 2, str(j)[:160])
check("ask stage uses the QUESTION prompt, not the drafting one",
      CALLS[0]["messages"][0]["content"] is router.PROMPT_QUESTION_SYSTEM)
check("ask stage still states the budget", "agent steps" in CALLS[0]["messages"][1]["content"])

check("bad stage -> 400",
      adm.post("/agent/prompt-help", json={"task": "x", "stage": "interrogate"}).status_code == 400)

CALLS.clear()
agent._chat = fake_chat
r = adm.post("/agent/prompt-help", json={
    "task": "an RPG", "stage": "draft",
    "answers": [{"q": "Which platform?", "a": "Godot, standalone"},
                {"q": "Skipped one?", "a": ""}]})
msg = CALLS[0]["messages"][1]["content"]
check("draft stage 200 with answers", r.status_code == 200 and r.get_json()["task"])
check("answered question reaches the model", "Which platform? -> Godot, standalone" in msg, msg[-300:])
check("skipped question is NOT sent", "Skipped one?" not in msg, msg[-300:])
check("answers are marked as overriding defaults", "OVERRIDE" in msg)
CALLS.clear()
adm.post("/agent/prompt-help", json={"task": "an RPG", "stage": "draft"})
check("no answers -> no clarification block", "OVERRIDE" not in CALLS[0]["messages"][1]["content"])
check("draft stage is exactly one model call — nothing to repair behind the operator's back",
      len(CALLS) == 1)
CALLS.clear()
adm.post("/agent/prompt-help", json={"task": "an RPG"})
check("stage defaults to draft (old callers keep working)",
      CALLS[0]["messages"][0]["content"] is router.PROMPT_HELPER_SYSTEM)

# a coder that is down must not strand the operator in the ask stage: the UI
# reads an empty question list as "nothing to ask" and drafts from the idea
agent._chat = dead_chat
r = adm.post("/agent/prompt-help", json={"task": "x", "stage": "ask"})
check("dead coder in ask stage -> 200 with no questions",
      r.status_code == 200 and r.get_json()["questions"] == [], f"{r.status_code} {r.get_json()}")
agent._chat = lambda messages, **kw: "I have no questions, this is clear enough."
r = adm.post("/agent/prompt-help", json={"task": "x", "stage": "ask"})
check("unparseable ask reply -> 200 with no questions",
      r.status_code == 200 and r.get_json()["questions"] == [])

# ---- gone for good ------------------------------------------------------------
# The gauntlet (build -> blind critic -> revise) was removed 2026-08-21. Nothing
# of it may resurface: no bar prompt, no rounds knob, no gauntlet key anywhere.
print("--- gauntlet removed ---")
for name in ("PROMPT_BAR_SYSTEM", "_parse_bar_lines", "GAUNTLET_ROUNDS",
             "GAUNTLET_ROUNDS_MAX", "_parse_gauntlet", "_gauntlet_critique",
             "_gauntlet_files", "_parse_gauntlet_verdict"):
    check(f"router.{name} no longer exists", not hasattr(router, name))
page = router.AGENT_PAGE
check("the /agent page has no gauntlet controls left",
      "gauntlet" not in page.lower() and 'id="gbar"' not in page and 'id="grounds"' not in page)
check("the /agent page has no quality-bar box in the helper card",
      'id="sugBar"' not in page and "quality bar" not in page.lower())

print("--- PROMPT_QUESTION_SYSTEM invariants ---")
qsys = router.PROMPT_QUESTION_SYSTEM
check("asks only what changes the build", "CHANGE WHAT GETS BUILT" in qsys)
check("bans re-asking what the idea already said", "already answers" in qsys)
check("bans open-ended interviewing", "open-ended interviewing" in qsys)
# the model does not reliably know a named game's mechanics — it guessed
# turn-based one run and real-time the next. The operator played it; ask them.
check("uncertain mechanics are ASKED about, not guessed",
      "Asking beats guessing" in qsys and "turn-based or real-time" in qsys)
check("bounded question count with a default-first option rule",
      "2 to 4 questions" in qsys and "sensible default" in qsys)
check("does not make them choose which parts of their idea survive",
      "which parts of their own" in qsys and "EMPHASISE" in qsys)

agent._chat = _orig_chat
print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
