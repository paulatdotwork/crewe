"""A SEARCH/REPLACE marker must never be written into a source file.

Live run 2026-08-25: the model emitted 7 SEARCH blocks but 8 "=======" lines.
The non-greedy _DIFF_BLOCK regex handed the extra separator over as the TAIL
of the replacement text, so it was spliced into csvjson.py — SyntaxError at
line 52. The inspector caught it and the final foreman tried to repair it and
failed, costing two extra rounds and still shipping a broken file. Models will
keep miscounting separators; the harness must not copy them into files.

Verifies:
  1. the exact malformed shape (one SEARCH, two separators) applies CLEANLY
  2. the result is valid Python, with no marker left behind
  3. a well-formed edit is unaffected
  4. content that still carries a marker is REJECTED, not stored
  5. a file that ALREADY had marker-like text is not rejected for keeping it
  6. the model is told what it did wrong
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

router.USERDATA_ROOT = tempfile.mkdtemp(prefix="conflict_markers.")
router.set_owner("conflict-markers-test-user")

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS  " if c else "  FAIL  ") + n + (f"  [{d}]" if d and not c else ""))

print("conflict markers / malformed SEARCH-REPLACE")

BASE = ("def build_arg_parser():\n"
        "    p.add_argument('--indent')\n"
        "    p.add_argument('--delimiter')\n"
        "    return p\n")

def merge(name, content, files):
    warns, mem = [], {"events": []}
    out = router._merge_output_file(name, content, files, set(files),
                                    warns, "step 1", mem)
    return out, warns, mem["events"]

# --- 1-2. the malformed shape from the live run -----------------------------
MALFORMED = ("csvjson.py\n```\n<<<<<<< SEARCH\n    p.add_argument('--indent')\n"
             "    p.add_argument('--delimiter')\n=======\n"
             "    p.add_argument('--indent')\n    p.add_argument('--pretty')\n"
             "    p.add_argument('--delimiter')\n=======\n>>>>>>> REPLACE\n```\n")
got = router._extract_files(MALFORMED, existing={"csvjson.py": BASE})
out, warns, _ = merge("csvjson.py", got["csvjson.py"], {"csvjson.py": BASE})
check("a miscounted separator no longer corrupts the file", out is not None, str(warns))
check("no conflict marker survives into the result",
      out is not None and not router._has_conflict_marker("csvjson.py", out), repr(out))
check("the edit still did its job", out is not None and "--pretty" in out, repr(out))
if out:
    try:
        compile(out, "csvjson.py", "exec")
        check("the result is valid Python", True)
    except SyntaxError as e:
        check("the result is valid Python", False, str(e))

# --- 3. a well-formed edit is untouched -------------------------------------
GOOD = ("csvjson.py\n```\n<<<<<<< SEARCH\n    p.add_argument('--delimiter')\n"
        "=======\n    p.add_argument('--delimiter')\n    p.add_argument('--quiet')\n"
        ">>>>>>> REPLACE\n```\n")
got = router._extract_files(GOOD, existing={"csvjson.py": BASE})
out, warns, _ = merge("csvjson.py", got["csvjson.py"], {"csvjson.py": BASE})
check("a well-formed edit still applies",
      out is not None and "--quiet" in out and "--delimiter" in out, str(warns))

# --- 4-6. the invariant: never STORE a marker -------------------------------
corrupt = BASE + "=======\n    p.add_argument('--x')\n"
out, warns, mem = merge("csvjson.py", corrupt, {"csvjson.py": BASE})
check("content carrying a marker is refused", out is None, repr(out))
check("the warning names the leftover marker",
      any("SEARCH/REPLACE marker" in w for w in warns), str(warns))
check("the model is told the separators must balance",
      any("======= separator" in e for e in mem), str(mem))

had = BASE + "=======\n"
out, _, _ = merge("csvjson.py", had + "extra = 1\n", {"csvjson.py": had})
check("a file that already had marker-like text is not blocked", out is not None, repr(out))

# --- 7. markdown setext headings are not conflict markers ------------------
# "Title\n=========" is a legitimate markdown H1. Treating it as debris would
# reject every README that uses setext headings.
md_h1 = "My Title\n=========\n\nSome prose.\n"
check("a markdown setext H1 is not debris",
      not router._has_conflict_marker("README.md", md_h1))
check("...but a real SEARCH marker in markdown still is",
      router._has_conflict_marker("README.md", "<<<<<<< SEARCH\nx\n"))
check("a bare separator in a CODE file is still debris",
      router._has_conflict_marker("a.py", "x = 1\n=======\ny = 2\n"))
out, warns, _ = merge("README.md", md_h1, {"README.md": "old prose\n"})
check("a README with setext headings is accepted", out is not None, str(warns))

# --- 8. a successful delete is not an "automated check" flag ---------------
warns2, mem2 = [], {"events": []}
router._merge_output_file("gone.py", "DELETE", {"gone.py": "x" * 50},
                          {"gone.py"}, warns2, "step 1", mem2)
check("deleting a file emits NO warning", warns2 == [], str(warns2))
check("...but is still recorded for later steps",
      any("deleted gone.py" in e for e in mem2["events"]), str(mem2["events"]))

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
