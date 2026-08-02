#!/usr/bin/env bash
# Run every suite against the router.py at the repo root.
# Each suite is a standalone script: throwaway user store, temp config,
# faked backends. No model server is needed and nothing outside the temp
# dirs is written. Exit 0 = all green.
cd "$(dirname "$0")"

FILES=(test_auth.py test_tenancy.py test_invite.py test_harden.py
       test_effort.py test_summarize.py test_cost.py
       test_backend_kinds.py test_compare.py test_compare_thinking.py
       test_truncation.py)

fail=0
for t in "${FILES[@]}"; do
  [ -f "$t" ] || { printf '%-26s MISSING\n' "$t"; fail=1; continue; }
  out=$(timeout 900 python3 "$t" 2>&1)
  rc=$?
  line=$(printf '%s\n' "$out" | grep -E '^[0-9]+ passed' | tail -1)
  [ -z "$line" ] && line="no summary (exit $rc)"
  printf '%-26s %s\n' "$t" "$line"
  if [ $rc -ne 0 ]; then
    fail=1
    printf '%s\n' "$out" | grep -E '^  FAIL' | head -10
  fi
done

echo
if [ $fail -eq 0 ]; then echo "all green"; else echo "SOME SUITES FAILED"; fi
exit $fail
