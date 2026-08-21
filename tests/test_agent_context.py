"""Agent context management + project instructions (offline, deterministic).

Why: the message list was the model's whole memory of a run and it only grew —
every file written and every observation stayed verbatim. One real run died
with an HTTP 400 at step 11 on an 8k-context coder (three whole files in the
history by then). Older turns are now condensed (head + tail survive, bulk
goes), the newest stay verbatim, and the budget follows the coder's real
window when it is known. A workspace's AGENTS.md / CLAUDE.md is briefed.

No model, no Docker: the coder is scripted and the workspace is a stub.
"""
import os
import sys
import tempfile

_AUDIT_TMP = tempfile.mkdtemp(prefix="crewe-audit-test-")
os.environ["CREWE_AGENT_AUDIT_DIR"] = _AUDIT_TMP
os.environ.pop("CREWE_AGENT_URL", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)
import agent

ok, bad = [], []


def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


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


class BigBackend(StubBackend):
    """Every command prints ~19 KB (a build log, a test run)."""

    def run(self, cmd, timeout=None):
        self.ran.append(cmd)
        return self.exits.get(cmd, 0), (f"{cmd}:" + "x" * 90 + "\n") * 200, ""


class BrokenReads(StubBackend):
    def read_file(self, path, max_bytes=200_000):
        raise OSError("disk on fire")


class ByteClipBackend(StubBackend):
    """Clips by BYTES like the real backends (docker cp / open().read(n))."""

    def read_file(self, path, max_bytes=200_000):
        c = self.files.get(path)
        if c is None:
            return None
        return c.encode("utf-8")[:max_bytes].decode("utf-8", "replace")


class SeedBackend(StubBackend):
    def restore(self, src_dir):
        return True


def scripted(replies):
    seen = {"calls": [], "n": 0}

    def fake_chat(messages, **kw):
        seen["calls"].append([dict(m) for m in messages])
        seen["n"] += 1
        return replies[min(seen["n"] - 1, len(replies) - 1)]

    agent._chat = fake_chat
    return seen


def run(replies, backend=None, max_steps=8, task="do the task", **kw):
    seen = scripted(replies)
    events = []
    res = agent.run_agent(task, max_steps=max_steps, plan=False,
                          backend=backend or StubBackend(),
                          on_event=events.append, **kw)
    return res, seen, events


def total(msgs):
    return sum(len(m["content"]) for m in msgs)


RUN = '```json\n{"action":"run","cmd":"make test"}\n```'
FINISH = '```json\n{"action":"finish","summary":"done"}\n```'
READ = '```json\n{"action":"read","path":"a.py"}\n```'

# ---- _condense_text ---------------------------------------------------------------
print("--- _condense_text ---")
check("short text untouched", agent._condense_text("x" * 700) == "x" * 700)
long = "H" * 1000 + "T" * 1000
c = agent._condense_text(long)
check("head and tail kept, middle replaced by a marker",
      c.startswith("H" * 400) and c.endswith("T" * 200) and "1400 chars of this older turn condensed" in c
      and len(c) < 800, f"len={len(c)}")

# ---- _condense_history --------------------------------------------------------------
print("--- _condense_history ---")
def history(turns=10):
    msgs = [{"role": "system", "content": "S" * 1000},
            {"role": "user", "content": "T" * 1000},
            {"role": "user", "content": "W" * 1000}]
    for i in range(turns):
        msgs.append({"role": "assistant", "content": f"A{i}:" + "a" * 2000})
        msgs.append({"role": "user", "content": f"O{i}:" + "o" * 5000})
    return msgs

msgs = history()
cond = {}
new, lvl, tot = agent._condense_history(msgs, 3, cond)
check("level 1: turns older than the newest 6 are condensed (8 messages)",
      new == 8 and lvl == 1 and sorted(cond) == list(range(3, 11)), f"{new} {lvl} {sorted(cond)}")
check("a condensed turn keeps its head (the action line) and its tail",
      msgs[3]["content"].startswith("A0:") and msgs[3]["content"].endswith("a" * 200)
      and "condensed" in msgs[3]["content"] and len(msgs[3]["content"]) < 800)
check("the newest 6 turns are verbatim",
      all(len(m["content"]) in (2003, 5003) for m in msgs[11:]))
check("the prefix (system, task, briefing) is never touched",
      all(len(m["content"]) == 1000 for m in msgs[:3]))
new2, lvl2, _ = agent._condense_history(msgs, 3, cond)
check("idempotent: a second pass condenses nothing", new2 == 0 and lvl2 == 0)

msgs = history()
cond = {}
new, lvl, tot = agent._condense_history(msgs, 3, cond, budget=25_000)
check("over budget: window shrinks to 2 turns (level 2) then heads only (level 3)",
      lvl == 3 and tot <= 25_000, f"lvl={lvl} total={tot}")
check("level 3 turns are ~200-char heads with a marker",
      msgs[3]["content"].startswith("A0:") and "older turn condensed to its head" in msgs[3]["content"]
      and len(msgs[3]["content"]) < 340, msgs[3]["content"][-60:])
w = agent._condense_text("WRITE: a.py\n```\n" + "x" * 1000 + "\n```")
check("a condensed WRITE keeps its fences balanced and says the write landed in full",
      w.count("```") % 2 == 0 and "a write landed in full" in w, w[:60] + " … " + w[-60:])
m3 = [{"role": "assistant", "content": "WRITE: b.py\n```\n" + "y" * 600},
      {"role": "user", "content": "OBS"}] * 4
cond3 = {}
agent._condense_history(m3, 0, cond3, budget=10, keep_recent=1)
check("a level-3 head cut inside a fence closes it",
      m3[0]["content"].count("```") % 2 == 0 and cond3.get(0) == 3, m3[0]["content"][-120:])
check("the newest 2 turns stay verbatim even at level 3",
      all(len(m["content"]) in (2003, 5003) for m in msgs[-4:]))
check("condensed map records the level reached per message",
      set(cond.values()) == {3} and sorted(cond) == list(range(3, 19)), str(sorted(set(cond.values()))))

msgs = history(3)
msgs.insert(3, {"role": "assistant", "content": "tiny"})
msgs.insert(4, {"role": "user", "content": "OBSERVATION (step 0/8):\nexit_code=0"})
cond = {}
new, lvl, _ = agent._condense_history(msgs, 3, cond, keep_recent=1)
check("short old turns are left alone (nothing to save)",
      3 not in cond and 4 not in cond and msgs[3]["content"] == "tiny", str(sorted(cond)))

# ---- the loop: a long run stays inside the budget -------------------------------------
print("--- loop: budget ---")
saved = agent.CTX_CHARS
agent.CTX_CHARS = 60_000
res, seen, events = run([RUN] * 13 + [FINISH], backend=BigBackend(), max_steps=14)
sizes = [total(c) for c in seen["calls"]]
check("run completes", res["done"] and res["steps"] == 14, str(res.get("steps")))
check("every model call fits the 60 KB budget (14 calls, 12 KB observations)",
      all(s <= 60_000 for s in sizes), str(sizes))
check("it would have been ~130 KB without condensation (13 x 10 KB observations)",
      sum(len(o) for o in [c[-1]["content"] for c in seen["calls"][1:]]) > 120_000)
notes = [e for e in events if e.get("t") == "harness" and "context: condensed" in e.get("note", "")]
check("condensation is announced once per level, not every step",
      1 <= len(notes) <= 3, str(len(notes)))
check("the announcement names history size and budget",
      notes and "KB, budget 60 KB" in notes[0]["note"], notes[0]["note"] if notes else "")
last = seen["calls"][-1]
check("the newest observation is verbatim (no condensation marker)",
      "condensed" not in last[-1]["content"] and len(last[-1]["content"]) > 9_000)
check("the prefix is identical on every call",
      all(c[:3] == seen["calls"][0][:3] for c in seen["calls"]))
check("result reports the count and the (unknown) window",
      res.get("condensed", 0) > 0 and res.get("ctx_tokens") is None, str(res.get("condensed")))
agent.CTX_CHARS = saved

res, seen, events = run([RUN] * 3 + [FINISH], backend=StubBackend(), max_steps=6)
check("a short run with small outputs condenses nothing",
      res.get("condensed") == 0 and not [e for e in events if "context: condensed" in e.get("note", "")])
w = [e for e in events if e.get("t") == "harness" and e.get("note", "").startswith("coder window")]
check("the window + budget are stated at run start",
      w and "unknown" in w[0]["note"] and f"{agent.CTX_CHARS // 1000} KB" in w[0]["note"], str(w))
res, seen, events = run([FINISH], coder={"ctx_tokens": 8192})
w = [e for e in events if e.get("t") == "harness" and e.get("note", "").startswith("coder window")]
check("a coder that states its window gets a budget sized to it (8192 -> 19 KB)",
      w and "8,192 tokens" in w[0]["note"] and "19 KB" in w[0]["note"], str(w))
check("result carries the window", res.get("ctx_tokens") == 8192)

# ---- re-read after condensation serves the content again ------------------------------
print("--- loop: re-read vs condensation ---")
saved = agent.CTX_CHARS
agent.CTX_CHARS = 40_000
sb = BigBackend(files={"a.py": "print(1)\n" * 400})
res, seen, events = run([READ] + [RUN] * 8 + [READ, FINISH], backend=sb, max_steps=12)
obs2 = seen["calls"][-1][-1]["content"]
check("second read of an unchanged file is served AGAIN once its first read was condensed",
      "print(1)\n" * 50 in obs2 and "unchanged — you already read" not in obs2, obs2[:120])
agent.CTX_CHARS = saved
sb = StubBackend(files={"a.py": "print(1)\n"})
res, seen, events = run([READ, RUN, READ, FINISH], backend=sb, max_steps=6)
obs2 = seen["calls"][3][-1]["content"]
check("...but while the first read is still verbatim, the short note is served",
      "unchanged — you already read a.py at step 1" in obs2, obs2[:120])
saved = agent.CTX_CHARS
agent.CTX_CHARS = 40_000
sb = BigBackend(files={"a.py": "print(1)\n" * 400})
res, seen, events = run([READ, RUN, READ] + [RUN] * 8 + [READ, FINISH], backend=sb, max_steps=14)
obs3 = seen["calls"][3][-1]["content"]
obs_last = seen["calls"][-1][-1]["content"]
check("a served 'unchanged' note keeps pointing at the real copy: once THAT is condensed, the content is served again",
      "unchanged — you already read a.py at step 1" in obs3
      and "print(1)\n" * 50 in obs_last and "unchanged" not in obs_last, obs_last[:100])
agent.CTX_CHARS = saved

# observations shrink with the window; an over-budget history is said out loud
res, seen, events = run([RUN, FINISH], backend=BigBackend(), coder={"ctx_tokens": 8192})
obs1 = seen["calls"][1][-1]["content"]
w = [e for e in events if e.get("t") == "harness" and e.get("note", "").startswith("coder window")]
check("an 8k-window coder gets ~3 KB observations instead of 12 KB",
      len(obs1) < 3600 and "chars omitted from the middle" in obs1
      and w and "observations clipped at 3 KB" in w[0]["note"], f"len={len(obs1)} {w}")
saved = agent.CTX_CHARS
agent.CTX_CHARS = 9_000
BIGWRITE = [f'WRITE: f{i}.py\n```\n' + "x" * 20_000 + "\n```" for i in range(5)]
res, seen, events = run(BIGWRITE + [RUN, FINISH], backend=BigBackend(), max_steps=8)
over = [e for e in events if e.get("t") == "warn" and "even after condensing" in e.get("note", "")]
check("when even level 3 cannot fit the budget (20 KB writes on a 9 KB budget), one warning says so",
      len(over) == 1 and res["done"], f"{len(over)} done={res.get('done')} steps={res.get('steps')}")
agent.CTX_CHARS = saved

# ---- project instructions ---------------------------------------------------------------
print("--- project instructions ---")
def briefing(seen):
    return seen["calls"][0][2]["content"]

res, seen, events = run([FINISH], backend=StubBackend(files={"AGENTS.md": "Use pytest.\nNever touch db/."}))
b = briefing(seen)
check("AGENTS.md rides in the workspace briefing",
      "PROJECT INSTRUCTIONS (from AGENTS.md — follow them):\nUse pytest.\nNever touch db/." in b, b[-200:])
check("...and is announced in the transcript",
      any("project instructions loaded from AGENTS.md (28 chars)" in e.get("note", "") for e in events),
      str([e.get("note") for e in events if e.get("t") == "harness"]))
res, seen, events = run([FINISH], backend=StubBackend(files={"CLAUDE.md": "Run make check."}))
check("CLAUDE.md is the fallback", "(from CLAUDE.md — follow them):\nRun make check." in briefing(seen))
res, seen, events = run([FINISH], backend=StubBackend(files={"AGENTS.md": "A", "CLAUDE.md": "C"}))
check("AGENTS.md wins when both exist", "(from AGENTS.md" in briefing(seen) and "(from CLAUDE.md" not in briefing(seen))
res, seen, events = run([FINISH], backend=StubBackend(files={"main.py": "x"}))
check("no instruction file -> nothing said", "PROJECT INSTRUCTIONS" not in briefing(seen)
      and not any("project instructions" in e.get("note", "") for e in events))
res, seen, events = run([FINISH], backend=StubBackend(files={"AGENTS.md": "   \n"}))
check("a blank file is ignored", "PROJECT INSTRUCTIONS" not in briefing(seen))
res, seen, events = run([FINISH], backend=StubBackend(files={"AGENTS.md": "R" * 6000}))
b = briefing(seen)
check("a huge file is clipped with a marker",
      "R" * 4000 + "\n…[instructions clipped]…" in b and "R" * 4001 not in b)
res, seen, events = run([FINISH], backend=ByteClipBackend(files={"AGENTS.md": "é" * 5000}))
b = briefing(seen)
check("a multibyte file over the char limit is clipped WITH the marker",
      "é" * 4000 + "\n…[instructions clipped]…" in b and "é" * 4001 not in b)
res, seen, events = run([FINISH], backend=ByteClipBackend(files={"AGENTS.md": "漢" * 8000}))
b = briefing(seen)
check("a file clipped by the backend's BYTE limit is marked and never ends in a broken char",
      "…[instructions clipped]…" in b and "\ufffd" not in b and "漢" * 3000 in b)
res, seen, events = run([FINISH], backend=StubBackend(files={"AGENTS.md": "Rule one\nRule two"}))
check("the transcript shows a preview of what was loaded",
      any("loaded from AGENTS.md (17 chars): Rule one Rule two…" in e.get("note", "") for e in events),
      str([e.get("note") for e in events if "instructions" in e.get("note", "")]))
sb = SeedBackend(files={"repo/AGENTS.md": "Repo rules", "repo/main.py": "x"})
res, seen, events = run([FINISH], backend=sb, seed_dir="/seed/anything")
check("a continued project that holds a clone keeps briefing the repo's own AGENTS.md",
      "(from repo/AGENTS.md — follow them):\nRepo rules" in briefing(seen), briefing(seen)[-160:])
res, seen, events = run([FINISH], backend=BrokenReads(files={"AGENTS.md": "x"}))
check("a backend that cannot read does not break the run",
      res["done"] and "PROJECT INSTRUCTIONS" not in briefing(seen))
sb = StubBackend(files={"repo/AGENTS.md": "Repo rules", "AGENTS.md": "outer"})
res, seen, events = run([FINISH], backend=sb, task="fix https://github.com/acme/widget please")
check("after a harness clone the repo's own AGENTS.md wins over the workspace root",
      "(from repo/AGENTS.md — follow them):\nRepo rules" in briefing(seen)
      and any(c.startswith("rm -rf repo && git clone") for c in sb.ran), briefing(seen)[-150:])
check("pure helper: (None, None) when nothing is there",
      agent._project_instructions(StubBackend()) == (None, None))

print(f"\n{len(ok)} passed, {len(bad)} failed")
if bad:
    print("FAILED:", bad)
sys.exit(1 if bad else 0)
