#!/bin/bash
# Install the code on the main branch of this repo into /opt/sc350cut (no apt, no config change), restart, check.
#   sudo ./deploy/update-code.sh
# Pulls origin/main first (fast-forward only; as the user who owns the repo), refuses to install from any other
# branch or from a repo with uncommitted code changes, keeps a backup of the installed code for rollback.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_USER=$(stat -c %U .)
g() { if [ "$(id -u)" = 0 ]; then sudo -u "$REPO_USER" git "$@"; else git "$@"; fi; }

[ "$(g rev-parse --abbrev-ref HEAD)" = main ] || { echo "not on main (git checkout main first)"; exit 1; }
[ -z "$(g status --porcelain --untracked-files=no -- sc350cut)" ] || { echo "uncommitted changes in sc350cut/"; exit 1; }
g pull --ff-only origin main
echo "installing $(g log --oneline -1)"

ts=$(date +%Y%m%d-%H%M%S)
cp -a /opt/sc350cut/sc350cut "/opt/sc350cut/sc350cut.bak-$ts"
rm -rf /opt/sc350cut/sc350cut
cp -r sc350cut /opt/sc350cut/
find /opt/sc350cut/sc350cut -name __pycache__ -prune -exec rm -rf {} +
systemctl restart sc350cut
ok=
for i in $(seq 1 30); do curl -sf http://127.0.0.1:8350/api/jobs >/dev/null && { ok=1; break; }; sleep 1; done
[ -n "$ok" ] || { echo "service did not answer; roll back (below) and check: journalctl -u sc350cut -n 50"; }
curl -sf http://127.0.0.1:8350/api/jobs | python3 -c "
import json, sys
d = json.load(sys.stdin); p = d.get('printer') or {}
print('installed. jobs:', len(d['jobs']), '| Xerox:', 'online' if p.get('online') else 'OFFLINE - ' + p.get('message', '?'))"
echo "Rollback: sudo rm -rf /opt/sc350cut/sc350cut && sudo cp -a /opt/sc350cut/sc350cut.bak-$ts /opt/sc350cut/sc350cut && sudo systemctl restart sc350cut"
