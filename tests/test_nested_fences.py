"""A file whose own contents contain code fences must survive extraction.

Regression for a real PowerShell build that could not converge. The model
wrote a correct README documenting three shell commands; the extractor was a
non-greedy ```...``` regex, so the README ended at its FIRST inner example,
the remaining examples became junk 'fileN.txt' blocks, and the truncated
README was rejected as "a fragment — kept the previous version". Every step
was then told it had been lazy when it had sent the whole file, so it retried
the same correct output and failed identically five steps running.

Verifies:
  1. fence LENGTH is honoured: ````…```` survives inner ``` fences intact
  2. a 3-in-3 nested README keeps its examples and produces no junk files
  3. absorption stops at the next file LABEL, so the file after a README is
     never swallowed
  4. block content is byte-exact, including the newline before the closing
     fence (a file one byte short is a rewrite that silently never matches)
  5. an unclosed fence (generation cut off) still yields what arrived
  6. terminal-command blocks are still skipped
  7. an EMPTY block for an existing file is refused as EMPTY, not mislabelled
     a lazy fragment — the wrong message is what made the loop repeat
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

router.USERDATA_ROOT = tempfile.mkdtemp(prefix="nested_fences.")

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS  " if c else "  FAIL  ") + n + (f"  [{d}]" if d and not c else ""))

def ex(text, existing=None):
    return router._extract_files(text, existing=existing)

print("nested fences / files that contain code fences")

# --- 1. four-backtick wrapping (the documented, unambiguous form) ------------
four = ("**`README.md`**\n````markdown\n# Docs\n\n```powershell\nGet-Item X\n```\n\n"
        "prose between examples\n\n```bash\nls -la\n```\n\nend\n````\n")
g = ex(four, {"README.md": "o" * 50})
check("4-backtick file survives its inner fences",
      list(g) == ["README.md"] and "```powershell" in g["README.md"]
      and "```bash" in g["README.md"] and "end" in g["README.md"], str(list(g)))

# --- 2. three-inside-three, which is what models actually emit ---------------
nested = ("**`README.md`**\n```markdown\n# Usage\n\n```powershell\n.\\run.ps1\n```\n\n"
          "then check it\n\n```powershell\nGet-Item HKLM:\\X\n```\n\ndone\n```\n")
g = ex(nested, {"README.md": "o" * 4000})
check("3-in-3 README produces NO junk fileN blocks",
      not any(k.startswith("file") and k[4:5].isdigit() for k in g), str(list(g)))
check("3-in-3 README keeps every example",
      "run.ps1" in g.get("README.md", "") and "Get-Item" in g.get("README.md", ""),
      repr(g.get("README.md", ""))[:200])

# --- 3. absorption must stop at the next label ------------------------------
mixed = ("**`README.md`**\n```markdown\n# Hi\n\n```sh\nls\n```\n```\n\n"
         "**`app.py`**\n```python\nX = 1\n```\n")
g = ex(mixed, {"README.md": "o" * 10, "app.py": "o" * 10})
check("the labelled file after a nested README is not swallowed",
      (g.get("app.py") or "").strip() == "X = 1", str({k: len(v) for k, v in g.items()}))
check("...and the README still keeps its own example",
      "ls" in g.get("README.md", ""), repr(g.get("README.md", "")))

# --- 4. byte-exactness ------------------------------------------------------
check("content keeps the newline before the closing fence",
      ex("**`a.py`**\n```python\nprint(1)\n```\n") == {"a.py": "print(1)\n"},
      repr(ex("**`a.py`**\n```python\nprint(1)\n```\n")))
body = "line1\nline2\n"
check("multi-line body is byte-exact",
      ex("**`b.py`**\n```python\n" + body + "```\n") == {"b.py": body},
      repr(ex("**`b.py`**\n```python\n" + body + "```\n")))

# --- 5-6. the old guarantees still hold -------------------------------------
check("unclosed fence still yields what arrived",
      "line3" in ex("**`big.py`**\n```python\nline1\nline2\nline3").get("big.py", ""))
check("terminal-command blocks are still skipped",
      ex("run this\n```bash\nnpm install\n```\n") == {})

# --- 7. an empty block is refused as EMPTY, not as a fragment ---------------
warns, mem = [], {"events": []}
out = router._merge_output_file("install.ps1", "", {"install.ps1": "x" * 500},
                                {"install.ps1"}, warns, "step 1", mem)
check("empty block for an existing file is refused", out is None)
check("the warning says EMPTY, not 'fragment'",
      any("EMPTY" in w for w in warns) and not any("fragment" in w for w in warns),
      str(warns))
check("the warning states nothing was deleted",
      any("no file was deleted" in w for w in warns), str(warns))
check("the model is pointed at the real delete protocol",
      any("only the word DELETE" in e for e in mem["events"]), str(mem["events"]))

# --- 8. underscores are FILENAME characters, not markdown emphasis ---------
# "line.replace([`*_#>]+ -> ' ')" turned test_csvjson.py into "test csvjson.py",
# so the label resolved to the DIFFERENT existing file csvjson.py. Every
# underscored filename silently landed on its neighbour.
proj = {"csvjson.py": "C" * 5000, "README.md": "R" * 2000}
for label, want in (('test_csvjson.py', 'test_csvjson.py'),
                    ('**`test_csvjson.py`**', 'test_csvjson.py'),
                    ('my_module.py', 'my_module.py'),
                    ('js/deep_thing.js', 'js/deep_thing.js')):
    got = list(ex(label + "\n```\nBODY\n```\n", proj))
    check(f"underscored label survives: {label}", got == [want], str(got))
check("markdown emphasis is still stripped",
      list(ex("_app.js_\n```\nBODY\n```\n", {"app.js": "x" * 10})) == ["app.js"],
      str(list(ex("_app.js_\n```\nBODY\n```\n", {"app.js": "x" * 10}))))
check("__bold__ is not mistaken for a filename",
      list(ex("__bold__\n```\nBODY\n```\n", proj)) == ["file1.txt"],
      str(list(ex("__bold__\n```\nBODY\n```\n", proj))))

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
