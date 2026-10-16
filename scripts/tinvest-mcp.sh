#!/bin/sh
# Launches the T-Invest MCP server. invest-public-api.tbank.ru is signed by the
# Russian Trusted Root CA (Ministry of Digital Development), which Node does not
# trust by default, so add it to NODE_EXTRA_CA_CERTS alongside any existing CA.
set -e
dir=$(cd "$(dirname "$0")/.." && pwd)
bundle="${TMPDIR:-/tmp}/tinvest-mcp-ca.pem"
{
  if [ -n "$NODE_EXTRA_CA_CERTS" ] && [ -f "$NODE_EXTRA_CA_CERTS" ]; then
    cat "$NODE_EXTRA_CA_CERTS"
    echo
  fi
  cat "$dir/certs/russian_trusted_ca.pem"
} > "$bundle"
export NODE_EXTRA_CA_CERTS="$bundle"
# Node's built-in fetch ignores HTTPS_PROXY unless this is set (no-op without a proxy).
export NODE_USE_ENV_PROXY=1
exec npx -y mcp-remote https://invest-public-api.tbank.ru/mcp \
  --header "Authorization:Bearer ${TINVEST_TOKEN}"
