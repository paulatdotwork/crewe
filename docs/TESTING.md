# Testing

Crewe's suite lives in `tests/` — eleven standalone scripts and a runner:

```bash
cd tests
bash run_all.sh
```

Each suite is plain Python (no pytest, no fixtures framework): it imports
`router.py` from the repo root, prints one `PASS`/`FAIL` line per check, and
exits non-zero on any failure. Run one directly with `python3 test_auth.py`.

## What you need

Python 3.10+ and the app's own dependencies:

```bash
pip install flask requests pypdf python-docx openpyxl
```

**No model server is required.** Every call a test makes is either against a
Flask test client or a faked backend — the suites monkeypatch functions like
`_stream_chat` and `requests.post` and then assert on *what Crewe sends and
accepts*, never on what a live model answers.

## The house rules

These keep the suite trustworthy; new tests should follow them.

- **Assert the contract, not the wiring.** A test asserts "the classifier
  prompt contains every route's rule line", never "the config has exactly
  these five routes". Suites that hardcoded an operator's setup broke every
  time the setup changed.
- **Never touch real state.** Every suite points `USERS_FILE`,
  `USERDATA_ROOT` and `ROUTER_CONFIG_FILE` at a temp directory before doing
  anything else. If `router_config.json` exists it is *copied* and the copy
  edited; on a fresh clone the built-in defaults are used. A test once wrote
  through to a live config — the guard rails date from that day.
- **Fake at the boundary.** Replace `_stream_chat` / `requests.post`, not
  internal helpers — the code between the route and the boundary is exactly
  what needs testing.
- **Test the counter-example.** Whenever a test asserts something fires, a
  sibling asserts it does NOT fire for the neighbouring case (a guest where
  an admin is required, a free backend where a paid one is priced, a closed
  fence where an open one triggers recovery).

## Layout

| Suite | Covers |
| --- | --- |
| `test_auth.py` | login, sessions, password change, admin gating |
| `test_tenancy.py` | per-user data isolation |
| `test_invite.py` | invites and account creation |
| `test_harden.py` | SSRF guards, header hygiene, input abuse |
| `test_effort.py` | fast/normal/extra coder tiers and budget sizing |
| `test_summarize.py` | summarize route, uploads (pdf/docx/xlsx), sessions |
| `test_cost.py` | paid backends, spend tracking, /help |
| `test_backend_kinds.py` | openai / llama.cpp / ollama / anthropic adapters |
| `test_compare.py` | compare mode and the judge |
| `test_compare_thinking.py` | judges with thinking models |
| `test_truncation.py` | cut-off replies, continuation, vision critic frame |

CI runs the same `run_all.sh` on every push and pull request
(`.github/workflows/tests.yml`).
