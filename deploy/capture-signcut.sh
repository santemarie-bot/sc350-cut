#!/bin/bash
# Record exactly what SignCut (in the Windows VM) sends to the SC-350, so
# this software can send the same commands.
#   sudo ./deploy/capture-signcut.sh       then cut one job from SignCut, press Ctrl+C
set -euo pipefail
cd "$(dirname "$0")"
CUTTER=${CUTTER:-192.168.0.52}
OUT=${OUT:-/srv/cut/signcut-capture-$(date +%Y%m%d-%H%M%S)}
command -v tcpdump >/dev/null || apt-get install -y tcpdump

echo "Recording traffic to $CUTTER. Send ONE job from SignCut now, wait until the cutter has finished, then press Ctrl+C."
tcpdump -i any -s 0 -w "$OUT.pcap" host "$CUTTER" || true
echo "Saved $OUT.pcap"
python3 pcap_payload.py "$OUT.pcap" "$CUTTER" "$OUT"
