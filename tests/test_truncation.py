"""Truncation recovery and the honest visual critic.

House style: no live backends — _stream_chat and requests.post are faked;
asserts the CONTRACT (what gets sent, what gets kept), never live wiring.
"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router

router.TRACE_ON = False          # tests must not write into ~/llama_logs/trace

P, F = [], []
def check(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (f"   [{detail}]" if detail and not cond else ""))

# ---------------------------------------------------------------- overlap trim
print("--- _overlap_trim ---")
head = "line one\nline two\nline three that is long enough\n"
check("continuation repeating the tail is deduplicated",
      router._overlap_trim(head, "line three that is long enough\nline four")
      == "line four")
check("no overlap -> continuation kept whole",
      router._overlap_trim(head, "completely new text here") ==
      "completely new text here")
check("tiny (<20 char) overlaps are not trimmed (too likely coincidence)",
      router._overlap_trim("abcdefgh\n", "gh\nrest of it") == "gh\nrest of it")
check("longest overlap wins, not the first short one",
      router._overlap_trim("aaa bbb ccc ddd eee fff ggg hhh\n",
                           "ddd eee fff ggg hhh\nnext") == "next")

# ---------------------------------------------------------- continuation calls
print("--- _continue_truncated ---")
calls = []
def fake_stream(url, messages, job_id, **kw):
    calls.append({"url": url, "messages": messages, "kw": kw})
    return fake_stream.reply
router._stream_chat = fake_stream
router.coder_url = lambda: "http://coder/v1/chat/completions"

COMPLETE = "index.html\n```html\n<!doctype html><p>hi</p>\n```\ndone"
CUT = "index.html\n```html\n<!doctype html>\n<body>\n<p>half a file"

mem = {"events": []}
out = router._continue_truncated(None, COMPLETE, "sys", "user", 1, mem)
check("closed fences -> untouched, NO call made",
      out == COMPLETE and not calls)

fake_stream.reply = " of text</p>\n</body>\n```\n"
out = router._continue_truncated(None, CUT, "sys", "user", 1, mem)
check("cut reply -> exactly one continuation call", len(calls) == 1)
check("splice closes the fence and keeps both halves",
      out == CUT + " of text</p>\n</body>\n```\n", out[-80:])
m = calls[0]["messages"]
check("continuation call carries system+user+partial-assistant+instruction",
      [x["role"] for x in m] == ["system", "user", "assistant", "user"]
      and m[2]["content"] == CUT and "cut off" in m[3]["content"])
check("continuation is labelled for the trace",
      calls[0]["kw"].get("label") == "step1-continue")
check("recovery is noted in job memory",
      any("recovered" in e for e in mem["events"]))
new = router._extract_files(out, existing={}, default_name="index.html")
check("spliced reply parses into the complete file",
      "index.html" in new and "half a file of text</p>" in new["index.html"])

calls.clear()
fake_stream.reply = "\n<p>half a file continues but ALSO gets cut"
mem2 = {"events": []}
out = router._continue_truncated(None, CUT, "sys", "user", 2, mem2)
check("continuation that ALSO truncates -> original kept for the drop guard",
      out == CUT)
w = []
dropped = router._drop_truncated({"index.html": "<p>half"}, out, 2, w, mem2)
check("...and _drop_truncated then discards as before",
      dropped == {} and any("cut off" in x for x in w))

calls.clear()
fake_stream.reply = "```html\n<!doctype html><p>restarted from scratch</p>\n```"
out = router._continue_truncated(None, CUT, "sys", "user", 3, {"events": []})
check("continuation that RESTARTS the file (own fences) -> rejected, original kept",
      out == CUT)

def boom(*a, **kw):
    raise RuntimeError("backend down")
router._stream_chat = boom
mem3 = {"events": []}
out = router._continue_truncated(None, CUT, "sys", "user", 4, mem3)
check("continuation call failing -> original out, noted, no raise",
      out == CUT and any("failed" in e for e in mem3["events"]))

# ------------------------------------------------------------- vision context
print("--- vision critic gets an honest frame description ---")
vs = router.VISION_SYSTEM
check("system: interaction-only states declared invisible",
      "after interaction" in vs and "never report them as missing" in vs)
check("system: empty state declared correct, not a defect",
      "empty state" in vs and "not defects" in vs)
check("system: still refuses to guess at behaviour",
      "Never guess" in vs)

posted = {}
class _FakeResp:
    status_code = 200
    text = json.dumps({"choices": [{"message": {"content": "LOOKS GOOD"}}]})
    class request:
        url = "http://v/v1/chat/completions"
    def json(self):
        return json.loads(self.text)
def fake_post(url, json=None, headers=None, timeout=None, **kw):
    posted["url"], posted["payload"] = url, json
    return _FakeResp()
router.requests.post = fake_post
router._vision_ready = lambda u: True
router._vision_url = lambda: "http://v/v1/chat/completions"

shot = os.path.join(tempfile.mkdtemp(), "s.png")
open(shot, "wb").write(b"\x89PNG fake")
defects = router._vision_critique(shot, "a time tracker", ["clock in/out"])
check("LOOKS GOOD -> no defects", defects == [])
utext = posted["payload"]["messages"][1]["content"][0]["text"]
check("critic is told the frame is FIRST LOAD with empty storage",
      "FIRST LOAD" in utext and "storage is empty" in utext, utext[:200])
check("critic is told nothing was clicked or typed",
      "clicked" in utext)
check("critic thinking is off (the compare-judge lesson)",
      posted["payload"].get("chat_template_kwargs", {}).get("enable_thinking")
      is False)
check("screenshot travels as an image part",
      posted["payload"]["messages"][1]["content"][1]["type"] == "image_url")

check("no screenshot -> critic abstains without calling the backend",
      router._vision_critique(None, "g", []) == [])

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
