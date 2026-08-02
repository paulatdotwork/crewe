"""/compare entrants must not lose their whole token budget to hidden reasoning.

Regression for the 53qwen26 report (2026-07-28): a thinking backend added to
/compare returned an empty pane or a single truncated sentence. Cause — every
other structured caller in router.py sends chat_template_kwargs
{enable_thinking: false}, but run_compare_backend (the per-entrant call) did
not, so a reasoning model spent all of max_tokens on reasoning_content and the
pane, which only collects delta["content"], showed nothing.

Offline by default. Set CREWE_LIVE_BACKEND=<chat-completions-url> to also drive
the real thing (e.g. http://192.168.0.30:8080/v1/chat/completions).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router

ok, bad = [], []


def check(name, cond, extra=""):
    (ok if cond else bad).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra and not cond else ""))


print("compare / thinking-model handling")

# --- 1. the entrant payload asks for thinking off ----------------------------
sent = {}


def fake_stream_into(url, payload, job_id, field, jobs=None, lock=None, route=None):
    sent["url"], sent["payload"], sent["field"] = url, payload, field
    return "an answer"


_real = router._stream_into
router._stream_into = fake_stream_into
try:
    with router.COMPARE_JOBS_LOCK:
        router.COMPARE_JOBS["j1"] = {
            "owner": "u", "order": ["53qwen26"], "mode": "backends",
            "judge": {"tokens": 0, "done": False, "answer": "", "started": True},
            "done": False,
            "53qwen26": {"tokens": 0, "done": False, "answer": "", "stage": "drafting"}}
    router.run_compare_backend("j1", "53qwen26", "http://example/v1/chat/completions",
                               "why is the sky blue?")
    kw = sent["payload"].get("chat_template_kwargs") or {}
    check("entrant sends chat_template_kwargs", bool(kw), sent["payload"].keys())
    check("entrant sends enable_thinking:false", kw.get("enable_thinking") is False, kw)
    check("entrant budget comes from the constant",
          sent["payload"].get("max_tokens") == router.COMPARE_MAX_TOKENS,
          sent["payload"].get("max_tokens"))
    check("entrant still streams", sent["payload"].get("stream") is True)
    pane = router.COMPARE_JOBS["j1"]["53qwen26"]
    check("pane filled and marked done", pane["answer"] == "an answer" and pane["done"])
finally:
    router._stream_into = _real
    with router.COMPARE_JOBS_LOCK:
        router.COMPARE_JOBS.pop("j1", None)

# both judges must keep theirs too (they already had it — guard against regress)
src = open(router.__file__).read()
check("compare judge keeps thinking off",
      src.count("enable_thinking") >= 10, src.count("enable_thinking"))


# --- 1b. output budget (raised from 1400 → 4000 on 2026-08-01) ---------------
# 1400 truncated any code or multi-part answer mid-sentence. Measured on
# Qwen3.6-27B with thinking off: an easy question finishes in ~300 tokens, a
# real comparison in ~940, but "design X then implement it" ran past 1400.
import inspect

check("entrant budget defaults to 4000", router.COMPARE_MAX_TOKENS == 4000,
      router.COMPARE_MAX_TOKENS)
check("entrant budget is env-tunable", "CREWE_COMPARE_MAX_TOKENS" in src)
check("judge excerpt defaults to 6000", router.COMPARE_JUDGE_EXCERPT == 6000,
      router.COMPARE_JUDGE_EXCERPT)
check("judge excerpt is env-tunable", "CREWE_COMPARE_JUDGE_EXCERPT" in src)

judge_src = inspect.getsource(router._check_compare_judge)
check("judge reads the constant, not a hardcoded slice",
      "COMPARE_JUDGE_EXCERPT" in judge_src and "[:2500]" not in judge_src)
entrant_src = inspect.getsource(router.run_compare_backend)
check("entrant reads the constant, not a hardcoded cap",
      "COMPARE_MAX_TOKENS" in entrant_src and "1400" not in entrant_src)

# The two are coupled: raising the answer budget without raising what the judge
# reads just means the judge grades a smaller fraction of what it compares. A
# typical full answer measured ~4100 chars, so the excerpt must clear that.
check("judge excerpt covers a typical full answer",
      router.COMPARE_JUDGE_EXCERPT >= 4500, router.COMPARE_JUDGE_EXCERPT)
# and the judge's own prompt must still fit its backend (26B on 8087, n_ctx 65536)
worst_case_chars = router.COMPARE_JUDGE_EXCERPT * router.COMPARE_MAX_BACKENDS
check("worst-case judge prompt fits a 65k-context judge",
      worst_case_chars / 4 < 60000, f"{worst_case_chars} chars")


# --- 2. a thinking-only stream is counted and explained, not shown blank -----
class _FakeResp:
    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        return iter(self._lines)


def _sse(deltas):
    out = []
    for d in deltas:
        out.append(b"data: " + json.dumps(
            {"choices": [{"delta": d, "index": 0}]}).encode())
    out.append(b"data: [DONE]")
    return out


_real_post = router.requests.post
_real_lines = router._lines
router._lines = lambda r: r.iter_lines()

try:
    # all reasoning, no content — the exact failure Pal saw
    router.requests.post = lambda *a, **k: _FakeResp(_sse(
        [{"reasoning_content": "thinking…"}] * 40))
    with router.COMPARE_JOBS_LOCK:
        router.COMPARE_JOBS["j2"] = {"m": {"tokens": 0, "done": False, "answer": ""}}
    out = router._stream_into("http://x/v1/chat/completions", {"messages": []}, "j2", "m")
    check("thinking chunks move the token counter",
          router.COMPARE_JOBS["j2"]["m"]["tokens"] == 40,
          router.COMPARE_JOBS["j2"]["m"]["tokens"])
    check("blank pane explains itself", "hidden reasoning" in out, out[:120])
    check("explanation names the real cause", "enable_thinking" in out)

    # a normal answer must be returned untouched, with no note bolted on
    router.requests.post = lambda *a, **k: _FakeResp(_sse(
        [{"content": "The sky is blue because shorter wavelengths scatter more. "}] * 6))
    with router.COMPARE_JOBS_LOCK:
        router.COMPARE_JOBS["j3"] = {"m": {"tokens": 0, "done": False, "answer": ""}}
    out = router._stream_into("http://x/v1/chat/completions", {"messages": []}, "j3", "m")
    check("normal answer passes through clean", "hidden reasoning" not in out, out[-80:])
    check("normal answer counted", router.COMPARE_JOBS["j3"]["m"]["tokens"] == 6)

    # a SHORT but genuine answer with no reasoning must not be annotated either
    router.requests.post = lambda *a, **k: _FakeResp(_sse([{"content": "42."}]))
    with router.COMPARE_JOBS_LOCK:
        router.COMPARE_JOBS["j4"] = {"m": {"tokens": 0, "done": False, "answer": ""}}
    out = router._stream_into("http://x/v1/chat/completions", {"messages": []}, "j4", "m")
    check("short non-thinking answer not annotated", out.strip() == "42.", out)
finally:
    router.requests.post = _real_post
    router._lines = _real_lines
    with router.COMPARE_JOBS_LOCK:
        for j in ("j2", "j3", "j4"):
            router.COMPARE_JOBS.pop(j, None)


# --- 3. live backend (opt-in) ------------------------------------------------
live = os.environ.get("CREWE_LIVE_BACKEND")
if live:
    print(f"  -- live: {live}")
    import requests as _rq
    # A question with enough substance to make a reasoning model think hard.
    # A trivial one does NOT reproduce: at the 1400-token entrant budget the
    # model can afford to think AND answer, so the bug only bites once the
    # question is real. Measured on Qwen3.6-27B: easy → 962 tokens and a full
    # answer; this one → 1400 tokens, 6025 chars of thinking, 43 chars of answer.
    q = ("Compare Postgres and SQLite for a small multi-user web app. "
         "Give a recommendation.")
    base = {"messages": [{"role": "system", "content": router.COMPARE_SYSTEM},
                         {"role": "user", "content": q}],
            "stream": False, "max_tokens": 1400}

    r = _rq.post(live, json=base, timeout=600).json()
    ch = r["choices"][0]
    before = (ch["message"].get("content") or "")
    think_before = len(ch["message"].get("reasoning_content") or "")
    fr_before = ch.get("finish_reason")

    r = _rq.post(live, json={**base, "chat_template_kwargs": {"enable_thinking": False}},
                 timeout=600).json()
    ch = r["choices"][0]
    after = (ch["message"].get("content") or "")
    think_after = len(ch["message"].get("reasoning_content") or "")
    fr_after = ch.get("finish_reason")

    print(f"     without flag: {len(before):5d} chars answer, "
          f"{think_before:5d} chars thinking, finish={fr_before}")
    print(f"     with flag:    {len(after):5d} chars answer, "
          f"{think_after:5d} chars thinking, finish={fr_after}")

    thinker = think_before > 0
    check("live: backend is in fact a thinking model", thinker, think_before)
    if thinker:
        # the bug, on the unflagged call: budget consumed, answer starved
        check("live: without the flag the answer is starved",
              fr_before == "length" and len(before.strip()) < 300,
              f"finish={fr_before} content={len(before)}")
        check("live: the flag suppresses reasoning entirely", think_after == 0,
              think_after)
        check("live: the flag yields a real answer",
              len(after.strip()) > 1000, len(after))
        check("live: the flag is a large improvement",
              len(after) > len(before) * 4, f"{len(before)} -> {len(after)}")

print(f"\n{len(ok)} passed, {len(bad)} failed")
raise SystemExit(1 if bad else 0)
