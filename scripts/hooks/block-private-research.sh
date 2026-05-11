#!/usr/bin/env bash
# Block any commit that introduces files under research/, killstack/,
# or any path containing "private". The public tradedesk repo must never
# carry private strategy research; see RAD-1016, RAD-1018, RAD-1538.
set -euo pipefail

staged="$(git diff --cached --name-only --diff-filter=ACMR)"
if [ -z "$staged" ]; then
  exit 0
fi

bad="$(printf '%s\n' "$staged" | grep -E '(^|/)(research|killstack)/|private' || true)"

if [ -n "$bad" ]; then
  echo "Blocked: private research content detected — commit to ig_trader instead" >&2
  echo "Offending paths:" >&2
  printf '  %s\n' $bad >&2
  exit 1
fi

exit 0
