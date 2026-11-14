#!/bin/bash
# Update the installed sc350-cut code from this folder (no apt, no config change), restart, and check it.
#   sudo ./deploy/update-code.sh
# 2026-11-14: Copies box + print log ("printed X of Y"), rename API, Customer printed next to the QR, and the
# prepare() fix that kept overwriting the Customer field. Then renames + relabels job ALT-261114-001 (wyntru_0).
set -euo pipefail
cd "$(dirname "$0")/.."
ts=$(date +%Y%m%d-%H%M%S)
cp -a /opt/sc350cut/sc350cut "/opt/sc350cut/sc350cut.bak-$ts"
rm -rf /opt/sc350cut/sc350cut
cp -r sc350cut /opt/sc350cut/
find /opt/sc350cut/sc350cut -name __pycache__ -prune -exec rm -rf {} +
systemctl restart sc350cut
for i in $(seq 1 30); do curl -sf http://127.0.0.1:8350/api/jobs >/dev/null && break; sleep 1; done
echo "installed (backup: /opt/sc350cut/sc350cut.bak-$ts)"

# job ALT-261114-001: owner's naming + Customer printed next to the QR (sheet remade; printed/cut state kept)
J=ALT-261114-001
curl -sf -X POST "http://127.0.0.1:8350/api/$J/rename" -H 'Content-Type: application/json' \
  -d '{"name": "wyntru_0 - 45mm round x200 - 130270647004_x5-copies.pdf"}' && echo
curl -sf -X POST "http://127.0.0.1:8350/api/$J/customer" -H 'Content-Type: application/json' -d '{"customer": "wyntru_0"}' && echo
curl -sf http://127.0.0.1:8350/api/jobs | python3 -c "
import json, sys
j = [x for x in json.load(sys.stdin)['jobs'] if x['id'] == '$J'][0]
print(j['id'], '|', j['name'], '| customer:', j['customer'], '| order copies:', j['copies'], '| printed', j.get('printed_sheets'), 'of', j['copies'])"
echo "Rollback: sudo rm -rf /opt/sc350cut/sc350cut && sudo cp -a /opt/sc350cut/sc350cut.bak-$ts /opt/sc350cut/sc350cut && sudo systemctl restart sc350cut"
