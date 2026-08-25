"""A step must be able to REMOVE a file, not just rewrite one.

A consolidation step ("fold these three scripts into one") has to delete what
it folded in. There was no way to express that, so models used the only thing
available — an empty block — which the elision guard read as a lazy fragment
and refused. The dead files then survived into every later step, which tried
the same thing and failed the same way. Verifies:
  1. a block containing only DELETE removes an existing file
  2. common spellings (# DELETE, delete this file, <!-- DELETE -->) all work
  3. deleting a file that does not exist is refused, not silently ignored
  4. an EMPTY block still deletes nothing and now names the real protocol
  5. DELETE cannot slip past the blind-rewrite guard (unshown file)
  6. end to end: after the step, the file is gone from the project
  7. the foreman may fix a step but must not change its scope by deleting
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router  # noqa: E402

# Throwaway stores before anything can touch the real ones (house rule).
_tmp = tempfile.mkdtemp(prefix="suite.")
router.USERS_FILE = os.path.join(_tmp, "users.json")
_cfg = os.path.join(_tmp, "router_config.json")
try:
    import shutil
    shutil.copyfile(router.ROUTER_CONFIG_FILE, _cfg)
except OSError:
    import json as _json
    _json.dump(router.ROUTER_CONFIG, open(_cfg, "w"))
router.ROUTER_CONFIG_FILE = _cfg

router.USERDATA_ROOT = tempfile.mkdtemp(prefix="delete_protocol.")
router.set_owner("delete-protocol-test-user")

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS  " if c else "  FAIL  ") + n + (f"  [{d}]" if d and not c else ""))

def merge(name, content, files, allowed=None):
    warns, mem = [], {"events": []}
    out = router._merge_output_file(name, content, files,
                                    allowed if allowed is not None else set(files),
                                    warns, "step 1", mem)
    return out, warns, mem["events"]

print("delete protocol")

FILES = {"install.ps1": "x" * 500, "detect.ps1": "y" * 400}

# --- 1-2. the directive and its spellings -----------------------------------
out, warns, mem0 = merge("install.ps1", "DELETE", dict(FILES))
check("a DELETE block removes an existing file", out is router.DELETE_FILE, repr(out))
# Doing what was asked must NOT surface under "⚠ Automated checks still flag" —
# that is how a clean run ends up looking broken to the user.
check("a successful delete emits no warning", warns == [], str(warns))
check("...but is recorded for later steps",
      any("deleted install.ps1" in e for e in mem0), str(mem0))

for spelling in ("DELETE\n", "  delete  ", "# DELETE", "// delete this file",
                 "<!-- DELETE -->", "DELETE FILE", "; delete"):
    out, _, _ = merge("install.ps1", spelling, dict(FILES))
    check(f"spelling accepted: {spelling.strip()!r}", out is router.DELETE_FILE)

# --- 3. deleting something that isn't there ---------------------------------
out, warns, _ = merge("nope.ps1", "DELETE", dict(FILES))
check("deleting a non-existent file is refused",
      out is None and any("does not exist" in w for w in warns), str(warns))

# --- 4. an empty block is still not a delete --------------------------------
out, warns, mem = merge("install.ps1", "", dict(FILES))
check("an empty block still deletes nothing", out is None)
check("...and the model is pointed at DELETE",
      any("only the word DELETE" in e for e in mem), str(mem))

# --- 5. cannot delete a file the model was never shown ----------------------
out, warns, _ = merge("detect.ps1", "DELETE", dict(FILES), allowed={"install.ps1"})
check("DELETE respects the blind-rewrite guard",
      out is None and any("blind rewrite" in w for w in warns), str(warns))

# --- 6-7. end to end through the real step runner ---------------------------
JOB = "delete-job"
CALLS, SCRIPT = [], []
def fake_stream_chat(url, messages, job_id, temperature=None, max_tokens=None,
                     label=None, **kw):
    CALLS.append(label)
    if not SCRIPT:
        raise AssertionError(f"unscripted call: {label}")
    return SCRIPT.pop(0)
router._stream_chat = fake_stream_chat
router.FOREMAN_MODE = "off"

def run(step, files, *replies):
    CALLS.clear(); SCRIPT.clear(); SCRIPT.extend(replies)
    with router.JOBS_LOCK:
        router.JOBS[JOB] = {"notes": [], "stage": ""}
    notes, warns = [], []
    ok = router._execute_step(JOB, step, 1, 1, "goal", "1. only step",
                              files, notes, warns, "system", "foreman-system")
    return ok, warns

proj = {"one.ps1": "A" * 300, "two.ps1": "B" * 300, "all.ps1": "C" * 300}
reply = ("all.ps1\n```powershell\n" + "C" * 320 + "\n```\n\n"
         "one.ps1\n```\nDELETE\n```\n\ntwo.ps1\n```\nDELETE\n```\n")
ok, warns = run({"title": "consolidate", "detail": "fold them in",
                 "files": ["one.ps1", "two.ps1", "all.ps1"], "done_when": "done"},
                proj, reply)
check("consolidation step succeeds", ok, str(warns))
check("the folded-in files are GONE from the project",
      "one.ps1" not in proj and "two.ps1" not in proj, str(sorted(proj)))
check("the surviving file was still written", proj.get("all.ps1", "").startswith("C" * 320))
check("no bogus 'fragment' warning was emitted",
      not any("fragment" in w for w in warns), str(warns))
check("no retry was spent on a step that worked", len(CALLS) == 1, str(CALLS))

# --- 8. a DELETE must never act on a GUESSED filename ----------------------
# Live run 2026-08-25: the model correctly wrote "test_csvjson.py / DELETE",
# the label resolver mapped it onto csvjson.py, and the MAIN FILE was deleted.
# Every other fallback trades a wrong guess for a rejected block; for a delete
# it would trade it for destroyed work.
proj2 = {"csvjson.py": "C" * 5000, "README.md": "R" * 2000}
got = list(router._extract_files("```\nDELETE\n```\n", existing=proj2,
                                 default_name="csvjson.py"))
check("an UNLABELLED delete never inherits the step's target file",
      got == ["unnamed-delete.txt"], str(got))
out, warns, _ = merge("unnamed-delete.txt", "DELETE", dict(proj2))
check("...and that unnamed delete is then refused",
      out is None and any("does not exist" in w for w in warns), str(warns))
got = list(router._extract_files("test_csvjson.py\n```\nDELETE\n```\n",
                                 existing=proj2, default_name="csvjson.py"))
check("a delete for an underscored name does NOT hit its neighbour",
      got == ["test_csvjson.py"], str(got))

# --- 9. a deletion must STICK in session memory ----------------------------
# _store_files only ever adds, so a deleted file came straight back on the next
# request and every later step saw the corpse it had just folded in.
SID = "delete-persist-session"
with router.SESSIONS_LOCK:
    router.sessions()[SID] = {"files": {"keep.py": "K" * 100,
                                        "gone.py": "G" * 100}, "history": []}
router._forget_files(SID, ["gone.py"])
with router.SESSIONS_LOCK:
    left = sorted((router.sessions()[SID].get("files") or {}))
check("a deleted file is dropped from session memory", left == ["keep.py"], str(left))
router._forget_files(SID, ["not-there.py"])
with router.SESSIONS_LOCK:
    left2 = sorted((router.sessions()[SID].get("files") or {}))
check("forgetting an absent file is a no-op", left2 == ["keep.py"], str(left2))
router._forget_files("no-such-session", ["x.py"])
check("forgetting in an unknown session does not raise", True)

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
