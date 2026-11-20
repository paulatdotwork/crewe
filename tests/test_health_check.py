"""Crewe's health check must notice the failures that went unseen in 2026.

/healthz only proves Flask is up. Found 2026-11-19: the summarizer had
returned '' for months, its server was down after a reboot, and every web
search came back empty — all silently. The health check exercises each piece
the way real requests do. This suite runs it against a fake fleet (a fixed
router config; fake model servers and SearXNG) and checks:
  - one probe per backend URL actually in use (shared URLs merged, unused
    backends skipped), and a metered API is never sent a chat request;
  - each failure class is caught: blank replies, refused connections, busy
    vs dead servers, a misrouting classifier, an empty summary, dead search
    engines, a rejected API key, missing Piper files, low disk;
  - "failing since" carries across runs and resets on recovery;
  - the report is saved, and shown to admins only (/admin card, JSON, the
    chat-header tag), escaped.
"""
import collections
import os
import sys
import tempfile
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router  # noqa: E402

# Throwaway stores before anything can touch the real ones (house rule).
_tmp = tempfile.mkdtemp(prefix="health_check.")
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


TMP = tempfile.mkdtemp(prefix="health_check.")
router.USERDATA_ROOT = os.path.join(TMP, "userdata")
os.makedirs(router.USERDATA_ROOT)
router.HEALTH_FILE = os.path.join(TMP, "health.json")
router._HEALTH = {}
for name in ("piper", "voice.onnx"):
    open(os.path.join(TMP, name), "w").close()
router.PIPER_BIN = os.path.join(TMP, "piper")
router.PIPER_VOICE = os.path.join(TMP, "voice.onnx")

def _route(backend):
    return {"backend": backend, "model": "", "subject": "", "persona": "",
            "custom": False, "color": ""}

CFG = {
    "backends": [
        {"id": "recipes-x", "url": "http://h-recipes:8080", "key": "", "model": ""},
        {"id": "brain-x", "url": "http://h-brain:8087", "key": "", "model": ""},
        {"id": "quick-x", "url": "http://h-brain:8087", "key": "", "model": "q"},
        {"id": "unused-x", "url": "http://h-unused:8082", "key": "", "model": ""},
        {"id": "cls-x", "url": "http://h-cls:8083", "key": "", "model": ""},
        {"id": "sum-x", "url": "http://h-sum:8084", "key": "", "model": ""},
        {"id": "claude-x", "url": "https://api.anthropic.com", "key": "sk-test",
         "model": "claude-sonnet-5", "kind": "anthropic", "paid": True},
    ],
    "routes": {"recipes": _route("recipes-x"), "general": _route("brain-x"),
               "code": _route("quick-x")},
    "roles": {"classifier": "cls-x", "summarizer": "sum-x", "coder": "claude-x",
              "coder_fast": "brain-x", "coder_quick": "quick-x"},
}
router._BUDGETS_READY = False          # don't probe coder windows on apply
router._apply_router_config(CFG)


class _R:
    def __init__(self, status=200, data=None):
        self.status_code, self._d, self.ok = status, data or {}, status < 400
    def json(self):
        return self._d
    def raise_for_status(self):
        if self.status_code >= 400:
            raise router.requests.HTTPError(str(self.status_code), response=self)

def _reply(text, finish="stop"):
    return _R(200, {"choices": [{"message": {"content": text}, "finish_reason": finish}]})

GOOD_SEARCH = {"results": [
    {"url": "https://stackoverflow.com/q/1", "engines": ["google", "bing"]},
    {"url": "https://docs.python-requests.org/", "engines": ["duckduckgo"]}],
    "unresponsive_engines": []}

def healthy():
    return {"chat": {}, "alive": {}, "cls": "recipes",
            "summary": "The user is making chickpea curry for six.",
            "search": GOOD_SEARCH, "images": ["https://img.example/1.jpg"],
            "models_status": 200}

S = healthy()
POSTS, GETS = [], []

def fake_post(url, json=None, headers=None, timeout=None, **kw):
    POSTS.append((url, json))
    host = urlparse(url).hostname
    system = ((json or {}).get("messages") or [{}])[0].get("role") == "system"
    if url == router.CLASSIFIER and system:
        return _reply(S["cls"])
    if url == router.SUMMARIZER and system:
        return _reply(S["summary"])
    how = S["chat"].get(host, "ok")
    if how == "refused":
        raise router.requests.ConnectionError("refused")
    if how == "timeout":
        raise router.requests.Timeout("timed out")
    if how == "empty":
        return _reply("", "length")
    if how == "boom":
        raise ValueError("unexpected reply shape")
    return _reply("ready")

def fake_get(url, params=None, headers=None, timeout=None, **kw):
    GETS.append((url, params, headers))
    if url == router.SEARXNG_URL:
        if (params or {}).get("categories") == "images":
            return _R(200, {"results": [{"img_src": u} for u in S["images"]]})
        return _R(200, S["search"])
    if url.endswith("/v1/models"):
        return _R(S["models_status"], {"data": []})
    if S["alive"].get(urlparse(url).hostname, True):
        return _R(200, {"status": "ok"})
    raise router.requests.ConnectionError("refused")

router.requests.post = fake_post
router.requests.get = fake_get

def run(**changes):
    S.clear(); S.update(healthy()); S.update(changes)
    POSTS.clear(); GETS.clear()
    rep = router.run_health_check()
    return rep, {c["key"]: c for c in rep["checks"]}

B = lambda host: next(f"backend:{u}" for u in
                      [t["url"] for t in router._health_targets()] if host in u)


print("A. a healthy fleet")
rep, c = run()
check("no fails or warnings", rep["fails"] == 0 and rep["warns"] == 0,
      [(k, v["status"], v["detail"]) for k, v in c.items() if v["status"] != "ok"])
backend_keys = sorted(k for k in c if k.startswith("backend:"))
check("one probe per backend URL in use", backend_keys == sorted([
    "backend:http://h-recipes:8080/v1/chat/completions",
    "backend:http://h-brain:8087/v1/chat/completions",
    "backend:http://h-cls:8083/v1/chat/completions",
    "backend:http://h-sum:8084/v1/chat/completions",
    "backend:https://api.anthropic.com/v1/messages"]), backend_keys)
check("unused backend skipped", not any("h-unused" in k for k in c))
brain = c[B("h-brain")]
check("shared URL merges its ids", brain["label"] == "Model server: brain-x, quick-x",
      brain["label"])
check("uses lists routes and roles", all(u in brain["uses"] for u in
      ("general", "code", "coder_fast", "coder_quick")), brain["uses"])
check("metered API never sent a chat request",
      not any("anthropic" in u for u, _ in POSTS))
check("metered API checked via its model list with the key",
      any(u == "https://api.anthropic.com/v1/models" and (h or {}).get("x-api-key") == "sk-test"
          for u, _, h in GETS))
probe = next(p for u, p in POSTS if "h-recipes" in u)
check("probe turns thinking off with room to answer",
      probe["chat_template_kwargs"] == {"enable_thinking": False}
      and probe["max_tokens"] >= 200, probe)
sum_call = next(p for u, p in POSTS if u == router.SUMMARIZER
                and p["messages"][0]["role"] == "system")
check("summarizer check sends the real summarizer request",
      sum_call["messages"][0]["content"] == router.SUMMARIZER_SYSTEM
      and sum_call["chat_template_kwargs"] == {"enable_thinking": False})
for key in ("routing", "summarizer", "search", "images", "voice", "disk"):
    check(f"{key} check present and ok", c.get(key, {}).get("status") == "ok", c.get(key))
check("search detail names the engines",
      "bing, duckduckgo, google" in c["search"]["detail"], c["search"]["detail"])
check("healthy checks have no 'since'", all(v["since"] is None for v in c.values()))
check("next check in the normal interval",
      router._health_next_delay(rep) == router.HEALTH_INTERVAL)

print("B. each failure is caught")
rep, c = run(summary="", chat={"h-sum": "empty"})
check("empty summary fails", c["summarizer"]["status"] == "fail"
      and "empty" in c["summarizer"]["detail"], c["summarizer"])
check("blank model reply fails with finish_reason",
      c[B("h-sum")]["status"] == "fail" and "nothing" in c[B("h-sum")]["detail"]
      and "length" in c[B("h-sum")]["detail"], c[B("h-sum")])
check("failures counted, retry sooner",
      rep["fails"] == 2 and router._health_next_delay(rep) == router.HEALTH_RETRY, rep["fails"])

rep, c = run(chat={"h-recipes": "refused"})
check("server down -> can't connect", c[B("h-recipes")]["status"] == "fail"
      and "can't connect" in c[B("h-recipes")]["detail"], c[B("h-recipes")])

rep, c = run(chat={"h-brain": "timeout"})
check("slow but alive -> busy warning", c[B("h-brain")]["status"] == "warn"
      and "busy" in c[B("h-brain")]["detail"], c[B("h-brain")])
check("a warning alone keeps the normal interval",
      rep["fails"] == 0 and router._health_next_delay(rep) == router.HEALTH_INTERVAL)
rep, c = run(chat={"h-brain": "timeout"}, alive={"h-brain": False})
check("slow and not alive -> fail", c[B("h-brain")]["status"] == "fail"
      and "not answering" in c[B("h-brain")]["detail"], c[B("h-brain")])

rep, c = run(cls="general")
check("misrouting classifier fails", c["routing"]["status"] == "fail"
      and "'general'" in c["routing"]["detail"], c["routing"])

rep, c = run(search={"results": [], "unresponsive_engines": [
    ["brave", "too many requests"], ["duckduckgo", "CAPTCHA"]]})
check("dead search fails and names the engines", c["search"]["status"] == "fail"
      and "brave: too many requests" in c["search"]["detail"]
      and "duckduckgo: CAPTCHA" in c["search"]["detail"], c["search"])
rep, c = run(search={"results": [{"url": "http:///go/v1/x", "engines": ["brave"]}],
                     "unresponsive_engines": []})
check("only broken links counts as no results", c["search"]["status"] == "fail", c["search"])
rep, c = run(search={"results": [{"url": "https://a.example/", "engines": ["bing"]}],
                     "unresponsive_engines": [["google", "CAPTCHA"]]})
check("one engine left -> warning naming it", c["search"]["status"] == "warn"
      and "only bing" in c["search"]["detail"] and "google: CAPTCHA" in c["search"]["detail"],
      c["search"])

rep, c = run(models_status=401)
check("rejected API key fails", c[B("anthropic")]["status"] == "fail"
      and "key rejected" in c[B("anthropic")]["detail"], c[B("anthropic")])

rep, c = run(images=[])
check("no photos -> warning", c["images"]["status"] == "warn", c["images"])

real_bin = router.PIPER_BIN
router.PIPER_BIN = os.path.join(TMP, "no-such-piper")
rep, c = run()
router.PIPER_BIN = real_bin
check("missing Piper fails", c["voice"]["status"] == "fail"
      and "no-such-piper" in c["voice"]["detail"], c["voice"])

DU = collections.namedtuple("DU", "total used free")
real_du = router.shutil.disk_usage
for free, want in ((1e9, "fail"), (8e9, "warn"), (500e9, "ok")):
    router.shutil.disk_usage = lambda p, f=free: DU(1000e9, 1000e9 - f, f)
    rep, c = run()
    check(f"disk {free / 1e9:g} GB free -> {want}", c["disk"]["status"] == want, c["disk"])
router.shutil.disk_usage = real_du

rep, c = run(chat={"h-recipes": "boom"})
check("an unexpected error fails that check only",
      c[B("h-recipes")]["status"] == "fail" and "ValueError" in c[B("h-recipes")]["detail"]
      and rep["fails"] == 1, c[B("h-recipes")])

print("C. 'failing since' across runs")
run()                                   # start from a clean slate
rep1, c1 = run(chat={"h-recipes": "refused"})
rep2, c2 = run(chat={"h-recipes": "refused"})
rep3, c3 = run()
rep4, c4 = run(chat={"h-recipes": "refused"})
k = B("h-recipes")
check("first failure stamps since", c1[k]["since"] == rep1["ts"])
check("still failing keeps the first since", c2[k]["since"] == rep1["ts"])
check("recovery clears since", c3[k]["since"] is None)
check("a new failure restarts since", c4[k]["since"] == rep4["ts"] != rep1["ts"])

print("D. saved, and shown to admins only")
saved = router._load_health()
check("report saved and reloads", saved.get("ts") == rep4["ts"]
      and saved.get("fails") == rep4["fails"])

router.app.config["SESSION_COOKIE_SECURE"] = False
def client(email, admin):
    u, err = router.create_user(email, "correct-horse-battery", is_admin=admin)
    assert u, err
    cl = router.app.test_client()
    cl.post("/login", data={"email": email, "password": "correct-horse-battery"})
    return cl
adm = client("health-admin@example.com", True)
usr = client("health-user@example.com", False)

router._HEALTH = {}
page = adm.get("/admin").get_data(as_text=True)
check("card before any run says so", 'id="health"' in page and "No check has run yet" in page)

run(chat={"h-recipes": "refused"}, search={"results": [], "unresponsive_engines": [
    ["<script>alert(1)</script>", "CAPTCHA"]]})
page = adm.get("/admin").get_data(as_text=True)
check("card shows failures first", page.index('tag fail') < page.index('tag ok'))
check("card shows the summary line", "2 failing" in page and "checks" in page, page[:0])
check("card escapes details", "<script>alert(1)" not in page and "&lt;script&gt;" in page)
check("card shows failing since", "since " in page)
j = adm.get("/admin/health.json").get_json()
check("admin JSON has the report", j.get("fails") == 2 and len(j.get("checks", [])) > 5, j.get("fails"))
r = usr.get("/admin/health.json")
check("non-admin is refused the JSON", r.status_code == 403 and b"checks" not in r.data, r.status_code)
r = usr.post("/admin/health/run")
check("non-admin can't trigger a run", r.status_code == 403, r.status_code)
before = router._HEALTH["ts"]
S.clear(); S.update(healthy())
r = adm.post("/admin/health/run")
check("run-now reruns and returns to the card",
      r.status_code == 302 and r.headers["Location"].endswith("/admin#health")
      and router._HEALTH["ts"] > before and router._HEALTH["fails"] == 0, r.status_code)
chat = adm.get("/", headers={"Accept": "text/html"}).get_data(as_text=True)
check("chat header has the health tag hook",
      'id="healthTag"' in chat and "fetch('/admin/health.json')" in chat)

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
