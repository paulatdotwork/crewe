"""Searches about what is next, latest or current must not be answered from
old pages.

Found 2026-11-19, live: "When is the next launch?" (after an Artemis answer
saying Artemis II had flown) was answered from pages written before that
launch, so it called Artemis II upcoming. The engines ranked old, well-linked
pages first, and the answer model knew neither today's date nor how old each
page was. This suite pins:
  - _wants_recent: next/latest/current/now/... questions, or this year or a
    later one, ask for recent pages; an earlier year makes it history;
  - _search_results: those search the past year first and are topped up from
    the unfiltered search when that leaves too few (evergreen pages stay
    reachable); everything else is one unfiltered search; SearXNG gets
    time_range only when asked;
  - _page_dates: a page's own published/updated dates from meta tags or
    JSON-LD (Wikipedia's is at the very end), never a future or junk date,
    "updated" only when it is later than "published";
  - the search context shows each fetched page's dates, and the answer
    prompt starts with today's date (with results and without);
  - run_job filters a recency question, and only that, including on the
    retry with the words as typed.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router  # noqa: E402

# Throwaway stores before anything can touch the real ones (house rule).
_tmp = tempfile.mkdtemp(prefix="search_recent.")
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


router.USERDATA_ROOT = tempfile.mkdtemp(prefix="search_recent.")
router.set_owner("search-recent-test-user")

# --- A. which questions want recent pages --------------------------------------
print("A. _wants_recent")
W = lambda q, typed="": router._wants_recent(q, typed or q, year=2026)
for q in ("Artemis program next launch date", "upcoming Marvel movies",
          "latest iPhone model", "newest Framework laptop",
          "who is the current UK prime minister", "is the Golden Gate Bridge still open",
          "weather in Portland Maine this weekend", "mortgage rates today",
          "iPhone 18 release date 2026", "World Cup 2030 host cities"):
    check(f"recent: {q!r}", W(q))
for q in ("how do volcanoes form", "Framework Laptop 16 review", "best pizza in Boston",
          "latest iPhone in 2019", "Tour de France 2025 winner",
          "2000 calorie meal plan", "Nextcloud setup guide", "currency exchange basics"):
    check(f"not recent: {q!r}", not W(q))
check("the typed words count too ('now' in the message, not the rewrite)",
      W("Framework Laptop 16 price", "How much does it cost now?"))
check("a year in the message makes a timeless rewrite current",
      W("Tour de France winner", "who won the tour de france in 2026"))
check("the default year is this year", router._wants_recent(
    f"best laptops {time.strftime('%Y')}", "best laptops"))

# --- B. SearXNG gets time_range only when asked ------------------------------------
print("B. _searxng_query / web_search")
class _Resp:
    def __init__(self, data):
        self._d = data
    def raise_for_status(self):
        pass
    def json(self):
        return self._d

def searx(fn):
    calls = []
    def fake_get(url, params=None, timeout=None, **kw):
        calls.append(params)
        return _Resp({"results": [{"url": "https://a.example/x", "title": "A"}]})
    real = router.requests.get
    router.requests.get = fake_get
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fn()
    finally:
        router.requests.get = real
    return calls

calls = searx(lambda: router.web_search("next eclipse"))
check("no time_range by default (the health check's call too)",
      calls == [{"q": "next eclipse", "format": "json"}], str(calls))
calls = searx(lambda: router.web_search("next eclipse", time_range="year"))
check("time_range is passed through",
      calls == [{"q": "next eclipse", "format": "json", "time_range": "year"}], str(calls))

def results(recent, filtered, unfiltered):
    calls = []
    def fake(q, time_range=None):
        calls.append(time_range)
        return [{"url": u} for u in (filtered if time_range else unfiltered)]
    real = router.web_search
    router.web_search = fake
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            got = router._search_results("q", recent)
    finally:
        router.web_search = real
    return [r["url"] for r in got], calls, buf.getvalue()

got, calls, _ = results(False, ["new"], ["a", "b"])
check("not recent: one unfiltered search", calls == [None] and got == ["a", "b"], f"{calls} {got}")
five = [f"n{i}" for i in range(5)]
got, calls, log = results(True, five, ["a"])
check("recent with enough past-year pages: one filtered search",
      calls == ["year"] and got == five, f"{calls} {got}")
check("...and it is logged", "[search] past year: 5 results" in log and "topped" not in log, log)
got, calls, log = results(True, ["n1", "b"], ["a", "b", "c"])
check("recent with too few: topped up from the unfiltered search, past-year first, no repeats",
      calls == ["year", None] and got == ["n1", "b", "a", "c"], f"{calls} {got}")
check("...and the top-up is logged", "past year: 2 results, topped up to 4" in log, log)
got, calls, _ = results(True, [], ["a", "b"])
check("recent with no past-year pages: the unfiltered results", got == ["a", "b"], str(got))

# --- C. a page's own dates --------------------------------------------------------------
print("C. _page_dates")
D = lambda html: router._page_dates(html, today="2026-11-20")
check("article meta tags", D('<meta property="article:published_time" content="2026-03-07T10:00:00Z">'
                             '<meta property="article:modified_time" content="2026-11-11T08:00:00Z">')
      == ("2026-03-07", "2026-11-11"))
check("content before property", D('<meta content="2026-09-09T19:58:15+00:00" property="og:updated_time">')
      == ("", "2026-09-09"))
check("name= and itemprop=, any case", D('<META NAME="pubdate" CONTENT="2025-01-02">'
                                       '<meta itemprop="dateModified" content="2025-06-01">')
      == ("2025-01-02", "2025-06-01"))
check("JSON-LD", D('<script type="application/ld+json">{"datePublished":"2019-05-14T05:01:52Z",'
                   '"dateModified":"2026-11-20T11:44:11Z"}</script>') == ("2019-05-14", "2026-11-20"))
check("JSON-LD at the end of a 1 MB page (where Wikipedia puts it)",
      D("<p>x</p>" * 130000 + '{"datePublished": "2019-05-14T05:01:52Z"}') == ("2019-05-14", ""))
check("the first date of a kind wins (the page before its related articles)",
      D('{"datePublished":"2026-01-25"} {"datePublished":"2020-01-01"}') == ("2026-01-25", ""))
check("updated on the day it was published says nothing new",
      D('<meta property="article:published_time" content="2026-06-04T18:34:47Z">'
        '<meta property="article:modified_time" content="2026-06-04T18:35:56Z">') == ("2026-06-04", ""))
check("updated before published is junk and dropped",
      D('{"datePublished":"2026-06-04","dateModified":"2025-01-01"}') == ("2026-06-04", ""))
check("a future date is an event, not the page's",
      D('<meta name="date" content="2027-03-01">{"dateModified":"2026-12-01"}') == ("", ""))
for name, html in (("no metadata", "<html><p>Posted 2026-11-01</p></html>"),
                   ("a <time> tag (any date on the page)", '<time datetime="2026-11-01">Nov 1</time>'),
                   ("a non-ISO date", '<meta name="date" content="November 1, 2026">'),
                   ("an unrelated meta tag", '<meta name="description" content="2026-11-01 news">')):
    check(f"nothing from {name}", D(html) == ("", ""), str(D(html)))

class _Page:
    status_code, is_redirect = 200, False
    def __init__(self, text, ctype="text/html; charset=utf-8"):
        self.text, self.headers, self.content = text, {"content-type": ctype}, b""
    def raise_for_status(self):
        pass
def fetch(page):
    saved = (router.requests.get, router._url_is_public, router._pdf_to_text)
    router.requests.get = lambda *a, **k: page
    router._url_is_public = lambda u: True
    router._pdf_to_text = lambda b: "pdf text"
    try:
        return router._fetch_page("https://news.example/a")
    finally:
        router.requests.get, router._url_is_public, router._pdf_to_text = saved
got = fetch(_Page('<head><meta property="article:published_time" content="2026-02-03">'
                  '<title>T</title></head><body><p>Launch moved.</p></body>'))
check("_fetch_page: an HTML page's text and dates", got == ("Launch moved.", "2026-02-03", ""), str(got))
check("_fetch_page: a PDF has text, no dates",
      fetch(_Page("", "application/pdf")) == ("pdf text", "", ""))
check("_fetch_page_text is still just the text", router._fetch_page_text("http://localhost:8087/") == "")

# --- D. the context and the prompt ---------------------------------------------------
print("D. search context")
PAGES = {"https://a.example": ("Artemis III is planned for 2027.", "2026-03-07", "2026-11-11"),
         "https://b.example": ("Old news.", "2025-12-01", ""),
         "https://c.example": ("No date here.", "", "")}
real_fetch = router._fetch_page
router._fetch_page = lambda u: PAGES.get(u, ("", "", ""))
try:
    ctx, src = router.build_search_context(
        [{"url": u, "title": u[8:9].upper(), "content": "snip"} for u in PAGES]
        + [{"url": "https://d.example", "title": "D", "content": "snippet only"}])
finally:
    router._fetch_page = real_fetch
blocks = ctx.split("\n\n")
check("a page with both dates shows both",
      "URL: https://a.example\nPage date: published 2026-03-07, updated 2026-11-11\nSnippet: snip" in blocks[0],
      blocks[0][:160])
check("a page with one date shows it", "Page date: published 2025-12-01\n" in blocks[1], blocks[1][:120])
check("a page with none, or not fetched, shows no date line",
      "Page date" not in blocks[2] and "Page date" not in blocks[3], blocks[2][:120])

# --- E. run_job ----------------------------------------------------------------------
print("E. run_job")
class _Stream:
    status_code = 200
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def raise_for_status(self):
        pass
    def iter_lines(self):
        yield ("data: " + json.dumps({"choices": [{"delta": {"content": "Answer [1]."}}]})).encode()
        yield b"data: [DONE]"
class _Reply:
    status_code = 200
    def __init__(self, c):
        self._c = c
    def raise_for_status(self):
        pass
    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}
class _T:
    def start(self):
        pass
HIT = [{"url": "https://a.example", "title": "A", "content": "snip"}]

def job(question, history=(), rewrite_to="unused", results_for=None):
    """The real search route; -> (searches as (query, time_range), answer prompt)."""
    sid = f"rc-{len(P) + len(F)}"
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
        searched.append((q, time_range))
        return (results_for or {}).get(q, [])
    saved = (router.requests.post, router.spawn_owned, router.web_search, router._fetch_page)
    router.requests.post = fake_post
    router.spawn_owned = lambda target=None, args=(), **kw: _T()
    router.web_search = fake_search
    router._fetch_page = lambda url: ("", "2026-11-11", "")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            router.run_job(jid, "search", question, sid)
    finally:
        (router.requests.post, router.spawn_owned, router.web_search,
         router._fetch_page) = saved
    return searched, seen.get("messages", [{}])[-1].get("content", "")

today = time.strftime("%B %-d, %Y")
searched, prompt = job("When is the next total solar eclipse?",
                       results_for={"When is the next total solar eclipse?": HIT})
check("a 'next' question searches the past year first, then tops up",
      searched == [("When is the next total solar eclipse?", "year"),
                   ("When is the next total solar eclipse?", None)], str(searched))
check("the answer prompt starts with today's date",
      prompt.startswith(f"Today is {today}. Live web search results"), prompt[:120])
check("...says results carry their page's date, and shows it",
      "each with its page's own date" in prompt and "Page date: published 2026-11-11" in prompt,
      prompt[:300])
searched, prompt = job("How do volcanoes form?", results_for={"How do volcanoes form?": HIT})
check("a timeless question: one unfiltered search", searched == [("How do volcanoes form?", None)],
      str(searched))
check("...and its prompt is dated too", prompt.startswith(f"Today is {today}."), prompt[:80])
searched, prompt = job("Who won the 2019 World Series?")
check("no results: the fallback prompt is dated",
      prompt.startswith(f"(Today is {today}. Web search returned no usable results"), prompt[:120])
LAUNCH = [{"q": "What's the latest on Artemis?", "a": "Artemis II flew in April 2026."}]
searched, _ = job("When is the next launch?", LAUNCH, rewrite_to="Artemis program next launch date",
                  results_for={"When is the next launch?": HIT})
check("the retry with the words as typed is filtered the same way",
      [s for s in searched if s[0] == "When is the next launch?"]
      == [("When is the next launch?", "year"), ("When is the next launch?", None)], str(searched))

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
