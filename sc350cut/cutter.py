"""HP-GL output for the Vulcan SC-350 and the ways to send it.

The SC-350 takes plain HP-GL. The commands SignCut adds for camera marks
and QR jobs are not published; they go in cfg.preamble once we have a
sample PLT from SignCut to copy them from.
"""
import math
import os
import socket
import subprocess
import termios
from pathlib import Path


def transform(paths, cfg, origin_xy=(0.0, 0.0)):
    """Sheet mm paths -> cutter mm paths (origin shift, optional mirror)."""
    ox, oy = origin_xy
    sign = -1 if cfg.mirror else 1
    return [[(sign * (x - ox), y - oy) for x, y in path] for path in paths]


def sort_paths(paths):
    """Cut inner shapes before the outlines that contain them, then go
    nearest-next to cut down on travel."""
    def bbox(p):
        xs = [q[0] for q in p]; ys = [q[1] for q in p]
        return min(xs), min(ys), max(xs), max(ys)

    def area(b):
        return (b[2] - b[0]) * (b[3] - b[1])

    items = sorted(paths, key=lambda p: area(bbox(p)))
    pos, out = (0.0, 0.0), []
    # cutting smallest first already keeps inner-before-outer; within
    # similar sizes pick the nearest start point.
    remaining = list(range(len(items)))
    while remaining:
        window = remaining[:20]
        best = min(window, key=lambda i: math.dist(pos, items[i][0]))
        remaining.remove(best)
        out.append(items[best])
        pos = items[best][-1]
    return out


def to_hpgl(paths, cfg):
    u = cfg.units_per_mm
    cmds = [cfg.preamble.strip()] if cfg.preamble.strip() else []
    cmds.append("SP1;")
    if cfg.speed:
        cmds.append(f"VS{cfg.speed};")
    if cfg.force:
        cmds.append(f"FS{cfg.force};")
    for path in sort_paths(paths):
        pts = [(round(x * u), round(y * u)) for x, y in path]
        # drop repeated points
        clean = [pts[0]]
        for p in pts[1:]:
            if p != clean[-1]:
                clean.append(p)
        if len(clean) < 2:
            continue
        cmds.append(f"PU{clean[0][0]},{clean[0][1]};")
        cmds.append("PD" + ",".join(f"{x},{y}" for x, y in clean[1:]) + ";")
    if cfg.postamble.strip():
        cmds.append(cfg.postamble.strip())
    return "\n".join(cmds) + "\n"


class CutterError(Exception):
    pass


_BAUD = {9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
         57600: termios.B57600, 115200: termios.B115200}


def send(data: str, cfg):
    dev = cfg.cutter_device
    raw = data.encode("ascii")
    try:
        if dev.startswith("tcp://"):
            host, _, port = dev[6:].partition(":")
            with socket.create_connection((host, int(port or 9100)), timeout=10) as s:
                s.sendall(raw)
            return f"sent {len(raw)} bytes to {host}"
        if dev.startswith("cups:"):
            r = subprocess.run(["lp", "-d", dev[5:], "-o", "raw"], input=raw,
                               capture_output=True, timeout=30)
            if r.returncode:
                raise CutterError(r.stderr.decode(errors="replace").strip())
            return r.stdout.decode().strip()
        if dev.startswith("dir:"):
            # USB stick / export folder: the machine runs PLT files standalone
            raise CutterError("use export() for folder output")
        fd = os.open(dev, os.O_WRONLY | os.O_NOCTTY)
        try:
            if "tty" in dev:
                attrs = termios.tcgetattr(fd)
                speed = _BAUD.get(cfg.cutter_baud, termios.B9600)
                attrs[4] = attrs[5] = speed
                attrs[2] |= termios.CRTSCTS | termios.CLOCAL
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
            view = memoryview(raw)
            while view:
                n = os.write(fd, view[:4096])
                view = view[n:]
        finally:
            os.close(fd)
        return f"sent {len(raw)} bytes to {dev}"
    except OSError as e:
        raise CutterError(f"{dev}: {e.strerror or e}")


def export(data: str, folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{name}.plt"
    p.write_text(data)
    return p
