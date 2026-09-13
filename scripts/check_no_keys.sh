#!/bin/sh
# Refuse a commit that carries a Stripe API key.
#
# Key exposure in a repository is the leading cause of Stripe key takeover,
# so this is a hook rather than a lint: it must run before the object exists,
# not after CI notices. Install with:
#
#     ln -sf ../../scripts/check_no_keys.sh .git/hooks/pre-commit
#
# A live key (sk_live_/rk_live_) is always fatal. A test key is a warning you
# must acknowledge, because a test key in history is still a habit that ships
# a live one eventually.
set -eu

staged=$(git diff --cached --name-only --diff-filter=ACM)
[ -z "$staged" ] && exit 0

live=$(printf '%s\n' "$staged" | xargs -I{} grep -lE '(sk|rk)_live_[A-Za-z0-9]{20,}' {} 2>/dev/null || true)
if [ -n "$live" ]; then
  echo "BLOCKED: a live Stripe key appears in:" >&2
  printf '  %s\n' $live >&2
  echo "Roll it now at https://dashboard.stripe.com/apikeys before anything else." >&2
  exit 1
fi

test=$(printf '%s\n' "$staged" | xargs -I{} grep -lE '(sk|rk)_test_[A-Za-z0-9]{20,}' {} 2>/dev/null || true)
if [ -n "$test" ]; then
  echo "BLOCKED: a Stripe test key appears in:" >&2
  printf '  %s\n' $test >&2
  echo "Keys belong in the environment, never in source. Re-run with" >&2
  echo "  git commit --no-verify" >&2
  echo "only if you are certain this is a placeholder." >&2
  exit 1
fi
exit 0
