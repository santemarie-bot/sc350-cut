"""Pull the TCP bytes sent to / from one host out of a tcpdump capture.

    python3 pcap_payload.py capture.pcap 192.168.0.52 OUTPREFIX

Writes OUTPREFIX.sent.bin (computer -> cutter) and OUTPREFIX.reply.bin
(cutter -> computer), one pair per TCP port, and prints a readable summary.
No tshark needed. Handles the link types tcpdump uses (Ethernet, Linux
cooked v1/v2 for -i any).
"""
import socket
import struct
import sys
from collections import defaultdict


def packets(path):
    with open(path, "rb") as f:
        head = f.read(24)
        magic = struct.unpack("<I", head[:4])[0]
        endian = "<" if magic in (0xA1B2C3D4, 0xA1B23C4D) else ">"
        linktype = struct.unpack(endian + "I", head[20:24])[0]
        while True:
            rec = f.read(16)
            if len(rec) < 16:
                return
            _, _, incl, _ = struct.unpack(endian + "IIII", rec)
            yield linktype, f.read(incl)


def ip_payload(linktype, frame):
    if linktype == 1:            # Ethernet
        off, proto = 14, struct.unpack("!H", frame[12:14])[0]
        if proto == 0x8100:      # VLAN
            off, proto = 18, struct.unpack("!H", frame[16:18])[0]
    elif linktype == 113:        # Linux cooked v1
        off, proto = 16, struct.unpack("!H", frame[14:16])[0]
    elif linktype == 276:        # Linux cooked v2
        off, proto = 20, struct.unpack("!H", frame[0:2])[0]
    elif linktype in (12, 101):  # raw IP
        off, proto = 0, 0x0800
    else:
        return None
    return frame[off:] if proto == 0x0800 else None


def main():
    path, host, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
    streams = defaultdict(list)   # (direction, cutter port) -> [(seq, data)]
    for lt, frame in packets(path):
        ip = ip_payload(lt, frame)
        if not ip or ip[9] != 6:
            continue
        ihl = (ip[0] & 15) * 4
        total = struct.unpack("!H", ip[2:4])[0]
        src, dst = socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20])
        tcp = ip[ihl:total]
        sport, dport, seq = struct.unpack("!HHI", tcp[:8])
        data = tcp[(tcp[12] >> 4) * 4:]
        if not data:
            continue
        if dst == host:
            streams[("sent", dport)].append((seq, data))
        elif src == host:
            streams[("reply", sport)].append((seq, data))
    if not streams:
        print(f"No TCP data to or from {host} in the capture.")
        return
    for (direction, port), segs in sorted(streams.items()):
        seen, out = set(), bytearray()
        for seq, data in segs:            # drop retransmissions, keep order
            if (seq, len(data)) in seen:
                continue
            seen.add((seq, len(data)))
            out += data
        name = f"{prefix}.{direction}.{port}.bin"
        open(name, "wb").write(out)
        text = out[:1500].decode("latin-1")
        shown = "".join(c if 32 <= ord(c) < 127 or c in "\n" else f"<{ord(c):02x}>" for c in text)
        print(f"\n===== {direction} port {port}: {len(out)} bytes -> {name}")
        print(shown)
        if len(out) > 1500:
            tail = out[-300:].decode("latin-1")
            print("\n ... last bytes:")
            print("".join(c if 32 <= ord(c) < 127 or c in "\n" else f"<{ord(c):02x}>" for c in tail))


if __name__ == "__main__":
    main()
