"""Paid backends, spend estimation, and the help page.

House style: throwaway user store, temp config file, Flask test client.
No provider is ever called — usage payloads are synthesised.
"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router

tmpdir = tempfile.mkdtemp()
router.USERS_FILE = os.path.join(tmpdir, "users.json")
router.app.config["SESSION_COOKIE_SECURE"] = False
# Never let a test write the real router_config.json — one already did.
import shutil as _sh
_cfgcopy = os.path.join(tmpdir, "router_config.json")
if os.path.exists(router.ROUTER_CONFIG_FILE):
    _sh.copyfile(router.ROUTER_CONFIG_FILE, _cfgcopy)
else:                       # fresh clone: no config yet -> built-in defaults
    json.dump(router.ROUTER_CONFIG, open(_cfgcopy, "w"))
router.ROUTER_CONFIG_FILE = _cfgcopy

P, F = [], []
def check(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (f"   [{detail}]" if detail and not cond else ""))

c = router.app.test_client()
admin, err = router.create_user("admin@example.com", "correct-horse-battery", is_admin=True)
assert admin, err
c.post("/login", data={"email": "admin@example.com", "password": "correct-horse-battery"})

# Direct calls to store accessors need an owner on this thread — inside a
# request Flask sets it, but these tests call record_spend() directly.
router.set_owner(admin["id"])

FREE = "http://127.0.0.1:8087/v1/chat/completions"
PAID = "https://openrouter.example/api/v1/chat/completions"

print("--- nothing is paid until you say so ---")
check("a local backend is not paid", not router._is_paid(FREE))
check("an unknown url is not paid", not router._is_paid("http://nope/v1/chat/completions"))
check("prices default to zero", router._prices(FREE) == (0.0, 0.0), str(router._prices(FREE)))

# register a paid backend directly in BACKEND_INFO (what _apply_router_config builds)
router.BACKEND_INFO[PAID] = {"key": "sk-test", "model": "some/model",
                             "paid": True, "price_in": 3.0, "price_out": 15.0}
check("a backend marked paid reads as paid", router._is_paid(PAID))
check("prices round-trip", router._prices(PAID) == (3.0, 15.0), str(router._prices(PAID)))

print("--- request shape ---")
p = router._with_usage({"messages": [], "stream": True}, FREE)
check("free backends are NOT asked for usage (request unchanged)",
      "stream_options" not in p, str(p))
p = router._with_usage({"messages": [], "stream": True}, PAID)
check("paid backends are asked to report usage",
      p.get("stream_options") == {"include_usage": True}, str(p))
p = router._with_usage({"messages": [], "stream": False}, PAID)
check("non-streaming requests are left alone", "stream_options" not in p, str(p))

print("--- cost maths ---")
open(router.spend_file(), "w").close()          # start from empty
cost = router.record_spend(FREE, "general", {"prompt_tokens": 1000, "completion_tokens": 1000})
check("a free backend records nothing and costs nothing", cost == 0.0, str(cost))
check("...and writes no spend line", os.path.getsize(router.spend_file()) == 0)

cost = router.record_spend(PAID, "code", {"prompt_tokens": 1_000_000,
                                          "completion_tokens": 1_000_000})
check("1M in + 1M out at $3/$15 = $18", abs(cost - 18.0) < 1e-6, str(cost))
cost = router.record_spend(PAID, "code", {"prompt_tokens": 500, "completion_tokens": 100})
check("small request costs a small amount",
      abs(cost - (500 * 3 + 100 * 15) / 1e6) < 1e-9, str(cost))

print("--- when the provider reports no usage ---")
cost = router.record_spend(PAID, "code", None, "x" * 4000, "y" * 400)
check("falls back to a character estimate rather than recording zero",
      cost > 0, str(cost))
recs = [json.loads(l) for l in open(router.spend_file())]
check("the estimate is flagged as an estimate", recs[-1]["estimated"] is True, str(recs[-1]))
check("real usage is NOT flagged as estimated", recs[0]["estimated"] is False, str(recs[0]))

print("--- summary ---")
s = router.spend_summary()
check("totals add up", abs(s["all"] - sum(r["cost"] for r in recs)) < 1e-4,
      f"{s['all']} vs {sum(r['cost'] for r in recs)}")
check("request count excludes the free one", s["requests"] == 3, str(s["requests"]))
check("estimated_any is surfaced", s["estimated_any"] is True)
check("recent is newest-first", s["recent"][0]["ts"] >= s["recent"][-1]["ts"])

print("--- a failing spend log never loses the answer ---")
_real = router.spend_file
router.spend_file = lambda uid=None: "/nonexistent-dir/spend.jsonl"
try:
    check("an unwritable spend log returns 0 instead of raising",
          router.record_spend(PAID, "code", {"prompt_tokens": 10, "completion_tokens": 10}) == 0.0)
finally:
    router.spend_file = _real

print("--- /spend endpoint ---")
r = c.get("/spend")
j = r.get_json()
check("/spend responds", r.status_code == 200, str(r.status_code))
check("reports per-level coder cost", set(j["coder_paid"]) == {"fast", "normal", "extra"}, str(j.get("coder_paid")))
# Don't assume the operator's fleet is all-local — they may have wired a paid
# backend. Assert the endpoint AGREES with the config instead.
check("reported coder cost matches what the config actually says",
      j["coder_paid"]["extra"] == router._is_paid(router.CODE_AGENT_URL) and
      j["coder_paid"]["normal"] == router._is_paid(router.CODE_AGENT_URL_FAST) and
      j["coder_paid"]["fast"] == router._is_paid(router.CODE_AGENT_URL_QUICK),
      str(j["coder_paid"]))
r = c.get("/spend", headers={"Accept": "application/json"})
check("/spend needs auth", router.app.test_client().get("/spend").status_code == 401)

print("--- neither effort level implies cost ---")
# extra paid, the local tiers free
router.CODE_AGENT_URL, router.CODE_AGENT_URL_FAST = PAID, FREE
router.CODE_AGENT_URL_QUICK = FREE
j = c.get("/spend").get_json()
check("extra paid + local tiers free is reported exactly",
      j["coder_paid"] == {"extra": True, "normal": False, "fast": False},
      str(j["coder_paid"]))
# the inverse: a cheap paid model on EASY, a big local one on HARD
router.CODE_AGENT_URL, router.CODE_AGENT_URL_FAST = FREE, PAID
j = c.get("/spend").get_json()
check("a paid middle tier is reported exactly (the inverted setup)",
      j["coder_paid"] == {"extra": False, "normal": True, "fast": False},
      str(j["coder_paid"]))
# both local
router.CODE_AGENT_URL = router.CODE_AGENT_URL_FAST = router.CODE_AGENT_URL_QUICK = FREE
j = c.get("/spend").get_json()
check("an all-local install reports nothing paid anywhere",
      not any(j["coder_paid"].values()) and not j["any_paid"],
      str(j))

print("--- settings validation ---")
cfg = json.loads(json.dumps(router.ROUTER_CONFIG))
cfg["backends"][0]["paid"] = True
cfg["backends"][0]["price_in"] = 0
cfg["backends"][0]["price_out"] = 0
r = c.post("/settings/config", json=cfg)
check("paid with no prices is rejected (spend would always read as free)",
      r.status_code == 400 and "paid" in r.get_data(as_text=True),
      r.get_data(as_text=True)[:140])
cfg["backends"][0]["price_in"] = "not-a-number"
cfg["backends"][0]["price_out"] = 15
r = c.post("/settings/config", json=cfg)
check("a junk price is coerced, not fatal", r.status_code == 200,
      r.get_data(as_text=True)[:140])
cfg["backends"][0]["paid"] = False
c.post("/settings/config", json=cfg)

print("--- help page ---")
r = c.get("/help")
body = r.get_data(as_text=True)
check("/help renders", r.status_code == 200, str(r.status_code))
for topic, needle in [("adding a backend", "Scan localhost"),
                      ("the default-model trap", "default model"),
                      ("custom routes", "subject"),
                      ("subject vs persona", "persona"),
                      ("effort", "Effort"),
                      ("paid models", "costs money"),
                      ("estimates are estimates", "estimate"),
                      ("attachments", "attach"),
                      ("where data lives", "crewe_userdata")]:
    check(f"help covers {topic}", needle.lower() in body.lower(), needle)
check("help does not claim any tier means paid",
      "No tier means" in body)
check("/help needs auth", router.app.test_client().get(
      "/help", headers={"Accept": "text/html"}).status_code in (302, 401))

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
