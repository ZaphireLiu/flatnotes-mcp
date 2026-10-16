#!/usr/bin/env bash
# Generate a new inbound token + its SHA-256 digest and print a tokens.yaml
# snippet.
#
#   ./scripts/new_token.sh agent-laptop-rw "main:read,write"
#   ./scripts/new_token.sh admin-internal "main:read,write,delete docs:read"
#
# The plaintext token is printed exactly once. Store it in your password
# manager / MCP client configuration; it cannot be recovered from the digest.
set -euo pipefail

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required" >&2
  exit 1
fi

name="${1:-}"
matrix="${2:-}"

if [[ -z "$name" || -z "$matrix" ]]; then
  echo "usage: $0 <name> <\"server:scope[,scope] ...\">" >&2
  echo "  e.g. $0 agent-laptop-rw \"main:read,write\"" >&2
  exit 1
fi

token="$(openssl rand -hex 32)"
digest="$(printf '%s' "$token" | sha256sum | cut -d' ' -f1)"

echo "plaintext token (store this now):"
echo "  $token"
echo
echo "tokens.yaml entry:"
echo "  - name: $name"
echo "    token_sha256: \"$digest\""
echo "    servers:"
for pair in $matrix; do
  server="${pair%%:*}"
  scopes="${pair#*:}"
  joined="$(printf '%s' "$scopes" | sed 's/,/, /g')"
  echo "      $server: [$joined]"
done
