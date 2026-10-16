#!/bin/sh
# Launches the T-Invest MCP server. invest-public-api.tbank.ru uses a certificate
# signed by the Russian Trusted Root CA (Ministry of Digital Development), which is
# not in Node's default trust store, so add it on top of any CAs already configured.
set -e
DIR=$(cd "$(dirname "$0")/.." && pwd)
BUNDLE=$(mktemp "${TMPDIR:-/tmp}/tinvest-ca.XXXXXX")
if [ -n "$NODE_EXTRA_CA_CERTS" ] && [ -f "$NODE_EXTRA_CA_CERTS" ]; then
  cat "$NODE_EXTRA_CA_CERTS" >> "$BUNDLE"
fi
cat "$DIR/certs/russian_trusted_ca.pem" >> "$BUNDLE"
export NODE_EXTRA_CA_CERTS="$BUNDLE"
# Node's built-in fetch (used by mcp-remote) ignores HTTPS_PROXY unless told otherwise.
if [ -n "$HTTPS_PROXY$https_proxy" ]; then
  export NODE_USE_ENV_PROXY=1
fi
exec npx -y mcp-remote https://invest-public-api.tbank.ru/mcp \
  --header "Authorization:Bearer ${TINVEST_TOKEN}"
