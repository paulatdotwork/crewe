"""The search route must not go quiet when SearXNG's engines do.

Found 2026-11-19: every web search had been returning nothing — brave
rate-limited us, duckduckgo and startpage served CAPTCHAs, google came back
empty — and the router silently fell back to model knowledge, so nothing in
router.log showed it. Fixed on the SearXNG side (newer image + engine set);
this suite pins the router side:
  - an empty answer is logged, naming SearXNG's unresponsive engines;
  - results with a host-less URL (brave's broken ``http:///go/v1/...``
    links) are dropped instead of taking a numbered slot;
  - transport errors still return [] and are logged.
Optional live probe (LIVE=1): the real SearXNG on 8888 returns results.
"""
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router  # noqa: E402

# Throwaway stores before anything can touch the real ones (house rule).
_tmp = tempfile.mkdtemp(prefix="web_search.")
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


class _Resp:
    def __init__(self, data):
        self._d = data
    def raise_for_status(self):
        pass
    def json(self):
        return self._d


def _run(data=None, exc=None):
    """web_search with requests.get faked; returns (results, printed)."""
    calls = []
    def fake_get(url, params=None, timeout=None, **kw):
        calls.append((url, params))
        if exc:
            raise exc
        return _Resp(data)
    real = router.requests.get
    router.requests.get = fake_get
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            out = router.web_search("current price of chickpeas at walmart")
    finally:
        router.requests.get = real
    return out, buf.getvalue(), calls


print("A. empty answers are visible in the log")
out, log, calls = _run({"results": [], "unresponsive_engines": [
    ["brave", "Suspended: too many requests"], ["duckduckgo", "CAPTCHA"]]})
check("empty -> []", out == [])
check("empty is logged", "[search] no results for" in log, log)
check("log names each unresponsive engine",
      "brave: Suspended: too many requests" in log and "duckduckgo: CAPTCHA" in log, log)
check("query sent as JSON", calls and calls[0][1] == {
    "q": "current price of chickpeas at walmart", "format": "json"}, calls)

out, log, _ = _run({"results": []})
check("empty with no engine report still logged",
      "[search] no results for" in log and "unresponsive" not in log, log)

out, log, _ = _run({"results": None, "unresponsive_engines": None})
check("null fields tolerated", out == [] and "[search] no results for" in log, log)

print("B. unusable result links are dropped")
good = {"url": "https://www.walmart.com/c/kp/chickpeas-canned", "title": "Canned Chickpeas",
        "engines": ["google"]}
broken = {"url": "http:///go/v1/Ae-XsHUVSZa0av9A3AH30RhkV80tzEh", "title": "x",
          "engines": ["brave"]}
blank = {"url": "", "title": "no link"}
missing = {"title": "no url key"}
out, log, _ = _run({"results": [broken, good, blank, missing], "unresponsive_engines": []})
check("only the real link survives", out == [good], out)
check("order kept, nothing logged when results remain", log == "", log)

out, log, _ = _run({"results": [broken, blank], "unresponsive_engines": []})
check("all-broken counts as empty and is logged",
      out == [] and "[search] no results for" in log, log)

print("C. transport failures")
out, log, _ = _run(exc=router.requests.ConnectionError("refused"))
check("connection error -> []", out == [])
check("connection error logged", "[search] searxng error" in log, log)

if os.environ.get("LIVE") == "1":
    print("D. live SearXNG")
    res = router.web_search("python requests timeout example")
    check("live search returns results", len(res) >= 5, len(res))
    check("live results all have hosts",
          all(router.urlparse(r["url"]).hostname for r in res))

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
