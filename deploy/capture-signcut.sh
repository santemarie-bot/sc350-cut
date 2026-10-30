#!/bin/bash
# Record exactly what SignCut (in the Windows VM) sends to the SC-350, so
# this software can send the same commands.
#   sudo ./deploy/capture-signcut.sh            then cut one job from SignCut, press Ctrl+C
set -euo pipefail
CUTTER=${CUTTER:-192.168.0.52}
OUT=${OUT:-/srv/cut/signcut-capture-$(date +%Y%m%d-%H%M%S)}
command -v tcpdump >/dev/null || apt-get install -y tcpdump
command -v nmap >/dev/null || apt-get install -y nmap

echo "Open ports on the cutter:"
nmap -Pn -p 1-10000,9100 --open "$CUTTER" | grep -E "^[0-9]+/" || echo "  (none found in 1-10000)"

echo "Recording traffic to $CUTTER on all interfaces. Cut one job in SignCut now, then press Ctrl+C."
tcpdump -i any -s 0 -w "$OUT.pcap" host "$CUTTER" || true
echo "Saved $OUT.pcap"
# pull the raw bytes the VM sent to the cutter out into a plain file
if command -v tshark >/dev/null; then
  tshark -r "$OUT.pcap" -Y "ip.dst==$CUTTER && tcp.len>0" -T fields -e tcp.payload | xxd -r -p > "$OUT.sent.bin"
  tshark -r "$OUT.pcap" -Y "ip.src==$CUTTER && tcp.len>0" -T fields -e tcp.payload | xxd -r -p > "$OUT.reply.bin"
  echo "SignCut sent:   $OUT.sent.bin"
  echo "Cutter replied: $OUT.reply.bin"
fi
