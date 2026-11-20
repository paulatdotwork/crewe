"""Mid-chat web searches must search for what the user means.

Found 2026-11-19: the search route sent each message to SearXNG verbatim.
Live, against the real engines: "How much does it cost now?" after a laptop
question returned an inflation calculator and dictionary pages, "and in
Portland, Maine?" after a weather question returned tourism sites, and "more"
returned the dictionary entry for "more". Now, when the chat has earlier
turns, the summarizer model rewrites the message into a standalone query.
This suite pins:
  - no history: no model call, the message is searched as typed;
  - the rewrite request: summarizer server, thinking off, short, dated, the
    last few exchanges only, Sources lists stripped, long text clipped;
  - reply cleanup, and every failure falls back to the message as typed;
  - run_job searches the rewrite, retries the typed words when the rewrite
    finds nothing, tells the model what it searched, and shows it under
    Sources only when the rewrite added words the user did not type;
  - one Sources list per answer: earlier answers replay without theirs
    (live, the model copied it into its follow-up answer, links from memory
    and all), and a trailing list the model writes anyway is cut, as is a
    one-line "Sources: [1] NASA, [2] ..." it closes with.
Optional live probe (LIVE=1): the real summarizer resolves a pronoun.
"""
import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router  # noqa: E402

# Throwaway stores before anything can touch the real ones (house rule).
_tmp = tempfile.mkdtemp(prefix="search_rewrite.")
router.USERS_FILE = os.path.join(_tmp, "users.json")
_cfg = os.path.join(_tmp, "router_config.json")
try:
    import shutil
    shutil.copyfile(router.ROUTER_CONFIG_FILE, _cfg)
except OSError:
    import json as _json
    _json.dump(router.ROUTER_CONFIG, open(_cfg, "w"))
router.ROUTER_CONFIG_FILE = _cfg
router.USERDATA_ROOT = _tmp

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS  " if c else "  FAIL  ") + n + (f"  [{d}]" if d and not c else ""))


router.USERDATA_ROOT = tempfile.mkdtemp(prefix="search_rewrite.")
router.set_owner("search-rewrite-test-user")

class _Reply:
    def __init__(self, content, status=200):
        self.status_code, self._c = status, content
    def raise_for_status(self):
        if self.status_code >= 400:
            raise router.requests.HTTPError(str(self.status_code))
    def json(self):
        return {"choices": [{"message": {"content": self._c}, "finish_reason": "stop"}]}

def rewrite(question, history, reply="x", exc=None, status=200):
    """_search_query with requests.post faked; -> (query, calls, printed)."""
    calls = []
    def fake_post(url, json=None, **kw):
        calls.append((url, json, kw))
        if exc:
            raise exc
        return _Reply(reply, status)
    real = router.requests.post
    router.requests.post = fake_post
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            q = router._search_query(question, history, today="November 19, 2026")
    finally:
        router.requests.post = real
    return q, calls, out.getvalue()

LAPTOP = [{"q": "Is the Framework Laptop 16 any good?",
           "a": "The Framework Laptop 16 is a modular 16-inch laptop.\n\n---\n"
                "**Sources**\n1. [Review](https://example.com/review)"}]

# --- A. when the rewrite runs, and what it sends ------------------------------
print("A. the rewrite request")
q, calls, _ = rewrite("How much does it cost now?", [])
check("no history: the message is searched as typed, no model call",
      q == "How much does it cost now?" and not calls)
q, calls, _ = rewrite("How much does it cost now?", LAPTOP, reply="Framework Laptop 16 price")
check("with history: the rewrite is used", q == "Framework Laptop 16 price", q)
url, body, kw = calls[0]
sysmsg, user = body["messages"][0]["content"], body["messages"][1]["content"]
check("it goes to the summarizer server (not the busy 8087 brain)", url == router.SUMMARIZER, url)
check("thinking off, deterministic, short",
      body["chat_template_kwargs"] == {"enable_thinking": False}
      and body["temperature"] == 0 and body["max_tokens"] <= 80 and not body["stream"],
      json.dumps({k: body[k] for k in ("temperature", "max_tokens", "stream")}))
check("it has a timeout", 0 < kw.get("timeout", 0) <= 30, kw.get("timeout"))
check("the system prompt carries today's date", "November 19, 2026" in sysmsg)
check("the earlier exchange and the latest message are shown",
      "Framework Laptop 16 any good" in user and "Latest message: How much does it cost now?" in user)
check("a search answer's Sources list is left out", "example.com/review" not in user
      and "**Sources**" not in user)

long_hist = [{"q": f"question {i} " + "q" * 2000, "a": f"answer {i} " + "a" * 5000}
             for i in range(8)]
q, calls, _ = rewrite("and the next one?", long_hist, reply="topic 7")
user = calls[0][1]["messages"][1]["content"]
check("only the last few exchanges are shown",
      "question 7" in user and "question 5" in user and "question 4" not in user)
check("long questions and answers are clipped (the summarizer has an 8k window)",
      len(user) < (router.SEARCH_REWRITE_Q_CHARS + router.SEARCH_REWRITE_A_CHARS + 40)
      * router.SEARCH_REWRITE_TURNS + 1700, len(user))

# --- B. cleaning the reply, and every failure falls back ----------------------
print("B. reply cleanup and fallbacks")
for raw, want in (('"Framework Laptop 16 price"', "Framework Laptop 16 price"),
                  ("Query: Framework Laptop 16 price", "Framework Laptop 16 price"),
                  ("Search query: `Framework Laptop 16 price`", "Framework Laptop 16 price"),
                  ("\n\nFramework Laptop 16 price\nThis searches for the price.", "Framework Laptop 16 price"),
                  ("<think>the user means the laptop</think>Framework Laptop 16 price",
                   "Framework Laptop 16 price")):
    q, _, _ = rewrite("How much does it cost now?", LAPTOP, reply=raw)
    check(f"cleaned: {raw[:38]!r}", q == want, repr(q))
ASKED = "How much does it cost now?"
for name, kw in (("connection refused", {"exc": router.requests.ConnectionError("refused")}),
                 ("timeout", {"exc": router.requests.Timeout("slow")}),
                 ("HTTP 500", {"status": 500}),
                 ("empty reply", {"reply": ""}),
                 ("only quotes", {"reply": '""'}),
                 ("an answer instead of a query", {"reply": " ".join(["word"] * 40)}),
                 ("one enormous line", {"reply": "x" * 400})):
    q, _, printed = rewrite(ASKED, LAPTOP, **kw)
    check(f"{name}: searched as typed, and logged", q == ASKED and "[search] query rewrite" in printed,
          f"{q!r} {printed!r}")

A = router._adds_context
check("shown: the rewrite brought in the subject", A("Framework Laptop 16 price", ASKED))
check("not shown: a trimmed copy of the message",
      not A("latest news bird flu outbreak", "What's the latest news on the bird flu outbreak?"))
check("not shown: the same words reordered, any case",
      not A("Walmart chickpeas price", "price of chickpeas at walmart?"))

# --- C. run_job uses it --------------------------------------------------------
print("C. run_job")
MODEL_SAYS = ["It costs $1,399 [1]."]     # what the fake answer model streams

class _Stream:
    status_code = 200
    headers = {"content-type": "text/event-stream"}
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def raise_for_status(self):
        pass
    def iter_lines(self):
        yield ("data: " + json.dumps({"choices": [{"delta": {"content": MODEL_SAYS[0]}}]})).encode()
        yield b"data: [DONE]"
    def close(self):
        pass

class _T:
    def start(self):
        pass

HIT = [{"url": "https://frame.work/laptop16", "title": "Framework Laptop 16", "content": "From $1,399"}]

def job(question, history, rewrite_to, results_for):
    """Run the real search route; -> (answer, searched queries, answer prompt)."""
    sid = f"rw-{len(P) + len(F)}"
    with router.SESSIONS_LOCK:
        router.sessions()[sid] = {"history": list(history)}
    jid = f"job-{sid}"
    with router.JOBS_LOCK:
        router.JOBS[jid] = {"owner": router._owner(), "tokens": 0, "done": False,
                            "route": "search", "answer": "", "stage": "drafting",
                            "audio": None, "stage_ts": 0}
    searched, seen = [], {}
    def fake_post(url, json=None, stream=False, **kw):
        if url == router.SUMMARIZER and not stream:
            return _Reply(rewrite_to)
        seen.setdefault("messages", json["messages"])
        return _Stream()
    def fake_search(q, time_range=None):
        searched.append(q)
        return results_for.get(q, [])
    saved = (router.requests.post, router.spawn_owned, router.web_search,
             router._fetch_page, router._wants_recent)
    router.requests.post = fake_post
    router.spawn_owned = lambda target=None, args=(), **kw: _T()
    router.web_search = fake_search
    router._fetch_page = lambda url: ("", "", "")
    # the past-year filter has its own suite (test_search_recent.py)
    router._wants_recent = lambda *a, **k: False
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            router.run_job(jid, "search", question, sid)
    finally:
        (router.requests.post, router.spawn_owned, router.web_search,
         router._fetch_page, router._wants_recent) = saved
    with router.JOBS_LOCK:
        ans = router.JOBS[jid]["answer"]
    return ans, searched, seen.get("messages", [])

ans, searched, msgs = job(ASKED, LAPTOP, "Framework Laptop 16 price",
                          {"Framework Laptop 16 price": HIT})
check("the rewrite is what gets searched", searched == ["Framework Laptop 16 price"], str(searched))
check("the model is told what was searched, and still sees the question as asked",
      'searched for "Framework Laptop 16 price"' in msgs[-1]["content"]
      and msgs[-1]["content"].endswith("Question: " + ASKED), msgs[-1]["content"][-160:])
check("Sources says what was searched",
      "**Sources** · searched for *Framework Laptop 16 price*" in ans, ans[-200:])

ans, searched, msgs = job(ASKED, LAPTOP, "Framework Laptop 16 price", {ASKED: HIT})
check("a rewrite that finds nothing is retried with the words as typed",
      searched == ["Framework Laptop 16 price", ASKED], str(searched))
check("...and then nothing claims a different search",
      "searched for" not in ans and "searched for" not in msgs[-1]["content"], ans[-200:])

ans, searched, msgs = job("Who won the 2026 Tour de France?", [], "unused",
                          {"Who won the 2026 Tour de France?": HIT})
check("first message of a chat: searched as typed, no note",
      searched == ["Who won the 2026 Tour de France?"] and "searched for" not in ans, str(searched))

ans, searched, _ = job("What's the latest news on the bird flu outbreak?", LAPTOP,
                       "latest news bird flu outbreak", {"latest news bird flu outbreak": HIT})
check("a trimmed copy is searched but not announced",
      searched == ["latest news bird flu outbreak"] and "searched for" not in ans, ans[-200:])

ans, _, _ = job(ASKED, LAPTOP, "Framework *Laptop* [16] `retail` price",
                {"Framework *Laptop* [16] `retail` price": HIT})
check("markdown in the shown query cannot break the Sources line",
      "searched for *Framework Laptop 16 retail price*" in ans, ans[-200:])

# --- C2. one Sources list per answer ---------------------------------------------
# live 2026-11-19: replayed answers end in our Sources list, and the model
# copied it into its follow-up answer — two lists, the first from memory.
print("C2. no copied Sources lists")
replay = router._history_messages(LAPTOP, 100000)
check("earlier answers replay without their Sources list",
      replay[1]["content"] == "The Framework Laptop 16 is a modular 16-inch laptop."
      and "example.com" not in json.dumps(replay), replay[1]["content"][-80:])
MODEL_SAYS[0] = ("It costs $1,399 [1].\n\n---\n**Sources**\n"
                 "1. [Framework](https://frame.work/made-up-from-memory)")
ans, _, msgs = job(ASKED, LAPTOP, "Framework Laptop 16 price",
                   {"Framework Laptop 16 price": HIT})
check("a Sources list the model wrote itself is cut; the real one stays",
      ans.count("**Sources**") == 1 and "made-up-from-memory" not in ans
      and "frame.work/laptop16" in ans and ans.startswith("It costs $1,399 [1]."), ans)
check("the follow-up's prompt carries no earlier Sources list",
      "example.com/review" not in json.dumps(msgs))
MODEL_SAYS[0] = "It costs $1,399 [1]."

D = router._drop_model_sources
body = "It costs $1,399 [1]."
for name, tail in (("our own format", "\n\n---\n**Sources**\n1. [A](https://a.example)\n2. [B](https://b.example)"),
                   ("a markdown heading", "\n\n## Sources\n- [A](https://a.example)"),
                   ("References: with bare URLs", "\n\nReferences:\n1. https://a.example\n2) https://b.example"),
                   ("**Sources:**", "\n\n**Sources:**\n* [A](https://a.example)"),
                   # live 11/20, in about 4 answers of 8, old prompt and new
                   ("one closing line", "\n\nSources: [1] NASA (2026-06-09), [2] Orbital Radar"),
                   ("one bold closing line", "\n\n---\n**Sources:** [3] Reuters (2026-10-05)")):
    check(f"cut: {name}", D(body + tail) == body, repr(D(body + tail)))
for name, text in (("prose after the list", body + "\n\nSources:\n1. [A](https://a.example)\n\nHope that helps!"),
                   ("a list without links", body + "\n\nSources:\n1. NASA\n2. ESA"),
                   ("the word in a sentence", "Good sources: NASA and ESA.\n1. [A](https://a.example)"),
                   ("no heading", body),
                   ("a sources line that is not the last",
                    body + "\n\nSources: [1] NASA, [2] ESA\n\nHope that helps!"),
                   ("a sources line without citations", body + "\n\nSources: NASA and ESA"),
                   ("an answer that is nothing but the line", "Sources: [1] NASA")):
    check(f"kept: {name}", D(text) == text, repr(D(text)))

# --- D. optional live probe ------------------------------------------------------
if os.environ.get("LIVE") == "1":
    print("D. live summarizer")
    router.set_owner(None)
    q = router._search_query("How much does it cost now?", LAPTOP)
    check("live: the pronoun is resolved to the laptop",
          "framework" in q.lower() and "16" in q, repr(q))

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
