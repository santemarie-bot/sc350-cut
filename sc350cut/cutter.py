"""HP-GL output for the Vulcan SC-350 and the ways to send it.

The SC-350 takes HP-GL wrapped in Vulcan CMD: commands (copied from
SignCut's Write-to-File output, see notes/signcut-protocol.md):

  IN;CMD:318,<len>,<wid>,200,200,1;PU;SP1; ...cuts... PU0,0;!PG;PU;CMD:319,<len>,<wid>,0;!PG;;

That is the "Write to File" form. The QR-mode job files SignCut keeps in
C:\\signcut\\<qr>.plt (notes/53061867.plt) are what we copy:

  IN;CMD:318,<mark span x>,<y>,600,600,0;CMD:307,100,100,<x>,<y>;PU;SP1; ...cuts...
  PU0,0;!PG;PU;CMD:319,<x+800>,<y>,0;!PG;;

with cut coordinates counted from the centre of the first mark.
CMD:318 feeds the sheet in and finds the marks, CMD:319 feeds it out.
Units are 0.025 mm. The cutter answers on the same connection (CMD:100
status, CMD:304 QR text, CMD:307 cut OK/NG), so the link is two-way.
"""
import math
import os
import socket
import subprocess
import termios
import time
from pathlib import Path


def transform(paths, cfg, origin_xy=(0.0, 0.0)):
    """Sheet mm paths -> cutter mm paths (origin shift, optional mirror)."""
    ox, oy = origin_xy
    sign = -1 if cfg.mirror else 1
    return [[(sign * (x - ox), y - oy) for x, y in path] for path in paths]


def rotate(paths, origin, sheet):
    """Turn a portrait sheet a quarter turn so its long side runs along the
    cutter's X (feed) axis. Returns (paths, origin, sheet) in the turned
    frame; mark 1 stays the bottom-left mark of the turned sheet."""
    W, H = sheet
    turn = lambda x, y: (y, W - x)
    ox, oy = origin
    # bottom-left mark of the turned sheet was the bottom-right one before
    new_origin = turn(W - ox, oy)
    return [[turn(x, y) for x, y in p] for p in paths], new_origin, (H, W)


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


def _units(mm, cfg):
    return round(mm * cfg.units_per_mm)


def feed_in(cfg, span):
    """CMD:318 as in SignCut's QR job files: the distance from mark 1 to the
    opposite mark along the feed (x) and across it (y), then feed_params.
    CMD:307 follows with the same mark rectangle."""
    sx, sy = (_units(v, cfg) for v in span)
    return f"CMD:318,{sx},{sy},{cfg.feed_params};CMD:307,100,100,{sx},{sy};"


def feed_out(cfg, span):
    sx, sy = (_units(v, cfg) for v in span)
    return f"CMD:319,{sx + 800},{sy},0;"


# what SignCut's spooler sends for a sheet with no job: feed it through
EMPTY_JOB = "IN;CMD:318,-200,-200,600,600,0;PU;CMD:319,16600,-200,0;!PG;;"


def mark_span(cfg, sheet):
    """Mark 1 to the far mark, in mm, for a sheet in the cutter's frame."""
    c = 2 * (cfg.mark_inset_mm + cfg.mark_size_mm / 2)
    return sheet[0] - c, sheet[1] - c


def cfg_sheet(cfg):
    """Configured sheet in the cutter's frame (long side along the feed)."""
    a, b = cfg.sheet_width_mm, cfg.sheet_height_mm
    return (max(a, b), min(a, b)) if cfg.rotate_portrait else (a, b)


def cut_body(paths, cfg):
    u = cfg.units_per_mm
    cmds = ["PU;SP1;"]
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
    return "".join(cmds)


def to_hpgl(paths, cfg, sheet=None):
    """One job as the cutter wants it. sheet = (x_mm, y_mm) in the cutter's
    frame: x runs along the feed (CMD:318 length), y across it (width)."""
    span = mark_span(cfg, sheet or cfg_sheet(cfg))
    body = cut_body(paths, cfg)
    if cfg.protocol == "signcut":
        return f"IN;{feed_in(cfg, span)}{body}PU0,0;!PG;PU;{feed_out(cfg, span)}!PG;;"
    pre, post = cfg.preamble.strip(), cfg.postamble.strip()
    return pre + body + post + "\n"


def strip_frame(data: str) -> str:
    """Cut commands only: drop IN, feed in/out and page commands, so a job
    file can be sent inside a QR-mode sheet that is already fed in."""
    data = data.strip().lstrip("$").rstrip("#")
    keep = []
    for c in data.split(";"):
        c = c.strip()
        if not c or c == "IN" or c.startswith(("CMD:318", "CMD:319", "CMD:307", "!PG")):
            continue
        keep.append(c + ";")
    return "".join(keep)


class CutterError(Exception):
    pass


_BAUD = {9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
         57600: termios.B57600, 115200: termios.B115200}


def _tcp(dev):
    host, _, port = dev[6:].partition(":")
    return host, int(port or 9100)


class Link:
    """Two-way TCP connection to the cutter."""

    def __init__(self, cfg, timeout=10):
        if not cfg.cutter_device.startswith("tcp://"):
            raise CutterError("two-way talk needs a tcp:// cutter_device")
        self.addr = _tcp(cfg.cutter_device)
        try:
            self.s = socket.create_connection(self.addr, timeout=timeout)
        except OSError as e:
            raise CutterError(f"{self.addr[0]}:{self.addr[1]}: {e.strerror or e}")
        self.buf = ""

    def close(self):
        self.s.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def send(self, text):
        self.s.sendall(text.encode("ascii"))

    def read(self, wait=1.0):
        """Whatever the cutter says within `wait` seconds of quiet."""
        self.s.settimeout(wait)
        try:
            while True:
                chunk = self.s.recv(4096)
                if not chunk:
                    break
                self.buf += chunk.decode("ascii", errors="replace")
        except socket.timeout:
            pass
        out, self.buf = self.buf, ""
        return out

    def expect(self, prefix, timeout):
        """Wait for a reply starting with prefix (e.g. "CMD:304,"); return
        the text after it up to the next ';', or None on timeout."""
        end = time.time() + timeout
        self.s.settimeout(0.5)
        while time.time() < end:
            i = self.buf.find(prefix)
            if i >= 0:
                j = self.buf.find(";", i)
                if j >= 0:
                    val = self.buf[i + len(prefix):j]
                    self.buf = self.buf[j + 1:]
                    return val
            try:
                chunk = self.s.recv(4096)
                if not chunk:
                    raise CutterError("cutter closed the connection")
                self.buf += chunk.decode("ascii", errors="replace")
            except socket.timeout:
                pass
        return None


def talk(text, cfg, wait=2.0):
    """Send text and return what the cutter answers (for testing)."""
    with Link(cfg) as link:
        link.send(text)
        return link.read(wait)


def send(data: str, cfg):
    dev = cfg.cutter_device
    data = data.strip().lstrip("$").rstrip("#")
    raw = data.encode("ascii")
    try:
        if dev.startswith("tcp://"):
            with Link(cfg) as link:
                link.send(data)
                reply = link.read(1.5)
            host = link.addr[0]
            return f"sent {len(raw)} bytes to {host}" + (f", cutter said {reply.strip()}" if reply.strip() else "")
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
    # SignCut wraps PLT files for the machine's own QR mode as $...#
    p.write_text("$" + data.strip() + "#")
    return p
