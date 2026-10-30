"""Turn artwork (SVG, PDF/EPS/AI via conversion, HP-GL/PLT) into cut paths.

A cut path is a list of (x, y) points in millimetres, origin bottom-left,
y pointing up (cutter coordinates).
"""
import math
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

SUPPORTED = {".svg", ".pdf", ".eps", ".ai", ".ps", ".plt", ".hpgl", ".hpg"}

# Curve flattening tolerance: segments per curve scale with length.
CURVE_STEP_MM = 0.25

UNIT_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72, "pc": 25.4 / 6,
           "px": 25.4 / 96, "": 25.4 / 96}


class ArtworkError(Exception):
    pass


# --------------------------------------------------------------------------
# affine transforms: (a, b, c, d, e, f) as in SVG matrix()

IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mul(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + c * B, b * A + d * B, a * C + c * D, b * C + d * D,
            a * E + c * F + e, b * E + d * F + f)


def apply(m, x, y):
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def parse_transform(s):
    m = IDENT
    if not s:
        return m
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", s):
        v = [float(t) for t in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", args)]
        if name == "matrix" and len(v) == 6:
            t = tuple(v)
        elif name == "translate":
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale":
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate":
            r = math.radians(v[0])
            t = (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0)
            if len(v) == 3:
                t = mul(mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        elif name == "skewX":
            t = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY":
            t = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        m = mul(m, t)
    return m


# --------------------------------------------------------------------------
# SVG path data

_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _steps(length):
    return max(2, min(400, int(length / CURVE_STEP_MM) + 1))


def _cubic(p0, p1, p2, p3, scale):
    est = (math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3)) * scale
    n = _steps(est)
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append((u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                    u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]))
    return out


def _quad(p0, p1, p2, scale):
    est = (math.dist(p0, p1) + math.dist(p1, p2)) * scale
    n = _steps(est)
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    return out


def _arc(p0, rx, ry, phi, large, sweep, p1, scale):
    # SVG implementation notes F.6.5
    if p0 == p1:
        return []
    if rx == 0 or ry == 0:
        return [p1]
    rx, ry = abs(rx), abs(ry)
    cp, sp = math.cos(math.radians(phi)), math.sin(math.radians(phi))
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1 = cp * dx + sp * dy
    y1 = -sp * dx + cp * dy
    lam = x1 * x1 / (rx * rx) + y1 * y1 / (ry * ry)
    if lam > 1:
        rx *= math.sqrt(lam)
        ry *= math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    co = math.sqrt(max(0, num / den)) if den else 0
    if large == sweep:
        co = -co
    cx1, cy1 = co * rx * y1 / ry, -co * ry * x1 / rx
    cx = cp * cx1 - sp * cy1 + (p0[0] + p1[0]) / 2
    cy = sp * cx1 + cp * cy1 + (p0[1] + p1[1]) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t1 = ang(1, 0, (x1 - cx1) / rx, (y1 - cy1) / ry)
    dt = ang((x1 - cx1) / rx, (y1 - cy1) / ry, (-x1 - cx1) / rx, (-y1 - cy1) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    n = _steps(abs(dt) * max(rx, ry) * scale)
    out = []
    for i in range(1, n + 1):
        t = t1 + dt * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        out.append((cp * x - sp * y + cx, sp * x + cp * y + cy))
    out[-1] = p1
    return out


def parse_path_d(d, scale=1.0):
    """Return list of polylines in the path's user units."""
    toks = _TOKEN.findall(d or "")
    i = 0
    subs, cur = [], []
    x = y = sx = sy = 0.0
    cmd = None
    last_ctrl = None
    last_cmd = None

    def num():
        nonlocal i
        v = float(toks[i])
        i += 1
        return v

    while i < len(toks):
        if toks[i].isalpha():
            cmd = toks[i]
            i += 1
        elif cmd is None:
            raise ArtworkError("bad path data")
        rel = cmd.islower()
        C = cmd.upper()
        if C == "Z":
            if cur:
                cur.append((sx, sy))
                subs.append(cur)
                cur = []
            x, y = sx, sy
            last_cmd, last_ctrl = "Z", None
            continue
        if i >= len(toks) or toks[i].isalpha():
            # command without arguments other than Z: skip
            continue
        ox, oy = (x, y) if rel else (0.0, 0.0)
        if C == "M":
            nx, ny = num() + ox, num() + oy
            if len(cur) > 1:
                subs.append(cur)
            cur = [(nx, ny)]
            x, y, sx, sy = nx, ny, nx, ny
            cmd = "l" if rel else "L"   # subsequent pairs are lineto
            last_ctrl = None
        elif C == "L":
            x, y = num() + ox, num() + oy
            cur.append((x, y))
            last_ctrl = None
        elif C == "H":
            x = num() + ox
            cur.append((x, y))
            last_ctrl = None
        elif C == "V":
            y = num() + oy
            cur.append((x, y))
            last_ctrl = None
        elif C in "CS":
            if C == "C":
                c1 = (num() + ox, num() + oy)
            else:
                c1 = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_cmd in "CS" and last_ctrl else (x, y)
            c2 = (num() + ox, num() + oy)
            p = (num() + ox, num() + oy)
            cur.extend(_cubic((x, y), c1, c2, p, scale))
            last_ctrl = c2
            x, y = p
        elif C in "QT":
            if C == "Q":
                c1 = (num() + ox, num() + oy)
            else:
                c1 = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_cmd in "QT" and last_ctrl else (x, y)
            p = (num() + ox, num() + oy)
            cur.extend(_quad((x, y), c1, p, scale))
            last_ctrl = c1
            x, y = p
        elif C == "A":
            rx, ry, phi = num(), num(), num()
            # flags may be packed like "0 01" -> handled since tokens split on numbers
            large, sweep = int(num()), int(num())
            p = (num() + ox, num() + oy)
            cur.extend(_arc((x, y), rx, ry, phi, large, sweep, p, scale))
            last_ctrl = None
            x, y = p
        if not cur:
            cur = [(x, y)]
        last_cmd = C
    if len(cur) > 1:
        subs.append(cur)
    return subs


# --------------------------------------------------------------------------
# SVG document

def _len_mm(s, default=None, unitless="px"):
    if s is None:
        return default
    m = re.match(r"\s*([-+]?[\d.]+(?:[eE][-+]?\d+)?)\s*([a-z%]*)", s)
    if not m or m.group(2) == "%":
        return default
    return float(m.group(1)) * UNIT_MM[m.group(2) or unitless] if (m.group(2) or unitless) in UNIT_MM \
        else float(m.group(1)) * UNIT_MM["px"]


def _style(el, inherited):
    st = dict(inherited)
    for k in ("stroke", "fill", "display", "visibility"):
        if el.get(k) is not None:
            st[k] = el.get(k).strip()
    for part in (el.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            st[k.strip()] = v.strip()
    return st


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _shape_polys(el, tag, scale):
    g = lambda k: float(el.get(k) or 0)  # noqa: E731
    if tag == "path":
        return parse_path_d(el.get("d"), scale)
    if tag == "rect":
        x, y, w, h = g("x"), g("y"), g("width"), g("height")
        rx = el.get("rx"); ry = el.get("ry")
        rx = float(rx) if rx else (float(ry) if ry else 0)
        ry = float(ry) if ry else rx
        rx, ry = min(rx, w / 2), min(ry, h / 2)
        if rx or ry:
            d = (f"M{x+rx},{y} H{x+w-rx} A{rx},{ry} 0 0 1 {x+w},{y+ry} V{y+h-ry} "
                 f"A{rx},{ry} 0 0 1 {x+w-rx},{y+h} H{x+rx} A{rx},{ry} 0 0 1 {x},{y+h-ry} "
                 f"V{y+ry} A{rx},{ry} 0 0 1 {x+rx},{y} Z")
            return parse_path_d(d, scale)
        return [[(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]]
    if tag in ("circle", "ellipse"):
        cx, cy = g("cx"), g("cy")
        rx = g("r") if tag == "circle" else g("rx")
        ry = g("r") if tag == "circle" else g("ry")
        n = _steps(2 * math.pi * max(rx, ry) * scale)
        return [[(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n))
                 for i in range(n + 1)]]
    if tag == "line":
        return [[(g("x1"), g("y1")), (g("x2"), g("y2"))]]
    if tag in ("polyline", "polygon"):
        v = [float(t) for t in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", el.get("points") or "")]
        pts = list(zip(v[0::2], v[1::2]))
        if tag == "polygon" and pts:
            pts.append(pts[0])
        return [pts] if len(pts) > 1 else []
    return []


NAMED = {"magenta": (255, 0, 255), "fuchsia": (255, 0, 255), "red": (255, 0, 0),
         "black": (0, 0, 0), "blue": (0, 0, 255), "lime": (0, 255, 0), "cyan": (0, 255, 255)}


def parse_color(s):
    s = (s or "").strip().lower()
    if s in NAMED:
        return NAMED[s]
    m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", s)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    m = re.fullmatch(r"rgb\(([^)]*)\)", s)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        if len(parts) == 3:
            return tuple(round(float(p[:-1]) * 2.55) if p.endswith("%") else round(float(p))
                         for p in parts)
    return None


def name_hit(names, text):
    """True if any name appears in text as whole words: 'cut' matches
    'Cut layer' and 'CutContour'... but not 'shortcut' or 'cutie'."""
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "").lower()
    words = " " + re.sub(r"[^a-z0-9]+", " ", words) + " "
    flat = words.replace(" ", "")
    for n in names:
        n = n.lower()
        if " " + re.sub(r"[^a-z0-9]+", " ", n).strip() + " " in words or \
                (len(n.replace(" ", "")) > 4 and n.replace(" ", "").replace("-", "") in flat):
            return True
    return False


class CutFilter:
    """Which lines in the artwork are cut lines.

    names: matched against element id/class and layer names (e.g. 'cutcontour').
    colors: stroke colours treated as cut lines; PDFs lose the spot colour name
    when converted, so the CutContour swatch is found by its colour instead.
    """

    def __init__(self, names=(), colors=(), tolerance=40):
        self.names = [n.lower() for n in names if n]
        self.colors = [c for c in (parse_color(x) for x in colors) if c]
        self.tolerance = tolerance

    def matches(self, el, st, layers):
        hay = " ".join([el.get("id") or "", el.get("class") or "", " ".join(layers)])
        if name_hit(self.names, hay):
            return True
        c = parse_color(st.get("stroke"))
        if c and any(max(abs(a - b) for a, b in zip(c, k)) <= self.tolerance for k in self.colors):
            return True
        return False


def _matches_cut(el, st, layer_names, cut_filter):
    if cut_filter is None:
        return True
    return cut_filter.matches(el, st, layer_names)


def svg_to_paths(svg_text, cut_filter=None, unitless="px"):
    """Parse SVG text -> (paths_mm, width_mm, height_mm). cut_filter: CutFilter or None (cut all)."""
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError as e:
        raise ArtworkError(f"SVG could not be read: {e}")
    vb = [float(t) for t in re.split(r"[\s,]+", (root.get("viewBox") or "").strip()) if t]
    w_mm = _len_mm(root.get("width"), unitless=unitless)
    h_mm = _len_mm(root.get("height"), unitless=unitless)
    if len(vb) == 4:
        vx, vy, vw, vh = vb
        sx = (w_mm / vw) if w_mm else UNIT_MM[unitless]
        sy = (h_mm / vh) if h_mm else sx
        w_mm = w_mm or vw * sx
        h_mm = h_mm or vh * sy
        base = (sx, 0, 0, sy, -vx * sx, -vy * sy)
    else:
        s = UNIT_MM[unitless]
        base = (s, 0, 0, s, 0, 0)
        w_mm = w_mm or 0
        h_mm = h_mm or 0

    defs = {}
    for el in root.iter():
        if el.get("id"):
            defs[el.get("id")] = el

    out = []

    def walk(el, m, st, layers, depth=0):
        tag = _local(el.tag)
        if tag in ("defs", "clipPath", "mask", "symbol", "metadata", "title", "desc",
                   "style", "pattern", "marker", "linearGradient", "radialGradient", "text"):
            return
        st = _style(el, st)
        if st.get("display") == "none":
            return
        m = mul(m, parse_transform(el.get("transform")))
        label = el.get("{http://www.inkscape.org/namespaces/inkscape}label") or el.get("id") or ""
        if tag == "g":
            for ch in el:
                walk(ch, m, st, layers + [label], depth)
            return
        if tag == "use" and depth < 8:
            href = el.get("{http://www.w3.org/1999/xlink}href") or el.get("href") or ""
            ref = defs.get(href.lstrip("#"))
            if ref is not None:
                mm = mul(m, (1, 0, 0, 1, float(el.get("x") or 0), float(el.get("y") or 0)))
                if _local(ref.tag) == "symbol":
                    for ch in ref:
                        walk(ch, mm, st, layers, depth + 1)
                else:
                    walk(ref, mm, st, layers, depth + 1)
            return
        if tag == "svg" and el is not root:
            for ch in el:
                walk(ch, m, st, layers, depth)
            return
        if st.get("stroke", "none") == "none" and st.get("fill", "black") == "none":
            return
        if not _matches_cut(el, st, layers, cut_filter):
            return
        scale = math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1
        for poly in _shape_polys(el, tag, scale):
            pts = [apply(m, px, py) for px, py in poly]
            if len(pts) > 1:
                out.append(pts)

    for ch in root:
        walk(ch, base, {}, [])

    # SVG y goes down; cutter y goes up.
    if not h_mm:
        h_mm = max((p[1] for path in out for p in path), default=0)
    if not w_mm:
        w_mm = max((p[0] for path in out for p in path), default=0)
    flipped = [[(x, h_mm - y) for x, y in path] for path in out]
    return flipped, w_mm, h_mm


# --------------------------------------------------------------------------
# HP-GL input (PLT files from design software)

def hpgl_to_paths(text, units_per_mm=40.0):
    paths, cur = [], []
    x = y = 0.0
    pen_down = False
    for cmd in re.split(r"[;\n\r]+", text):
        cmd = cmd.strip()
        if len(cmd) < 2:
            continue
        op, arg = cmd[:2].upper(), cmd[2:]
        nums = [float(t) for t in re.findall(r"[-+]?\d*\.?\d+", arg)]
        pairs = list(zip(nums[0::2], nums[1::2]))
        if op in ("PU", "PD", "PA"):
            if op == "PU":
                pen_down = False
                if len(cur) > 1:
                    paths.append(cur)
                cur = []
            elif op == "PD":
                pen_down = True
            for px, py in pairs:
                if pen_down:
                    if not cur:
                        cur = [(x / units_per_mm, y / units_per_mm)]
                    cur.append((px / units_per_mm, py / units_per_mm))
                x, y = px, py
            if op == "PU" and pairs:
                cur = []
        elif op == "IN":
            if len(cur) > 1:
                paths.append(cur)
            cur = []
    if len(cur) > 1:
        paths.append(cur)
    w = max((p[0] for pa in paths for p in pa), default=0)
    h = max((p[1] for pa in paths for p in pa), default=0)
    return paths, w, h


# --------------------------------------------------------------------------

def _pdf_to_svg(src: Path) -> str:
    if not shutil.which("pdftocairo"):
        raise ArtworkError("pdftocairo is missing (sudo apt install poppler-utils)")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.svg"
        r = subprocess.run(["pdftocairo", "-svg", "-f", "1", "-l", "1", str(src), str(out)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not out.exists():
            raise ArtworkError(f"PDF conversion failed: {r.stderr.strip()[:200]}")
        return out.read_text(errors="replace")


def _ps_to_pdf(src: Path, dst: Path):
    if shutil.which("ps2pdf"):
        r = subprocess.run(["ps2pdf", "-dEPSCrop", str(src), str(dst)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return
    raise ArtworkError("EPS/PS conversion needs ghostscript (sudo apt install ghostscript)")


def load_artwork(src: Path, cut_filter=None):
    """Return (paths_mm, width_mm, height_mm, note)."""
    ext = src.suffix.lower()
    note = ""
    if ext in (".plt", ".hpgl", ".hpg"):
        paths, w, h = hpgl_to_paths(src.read_text(errors="replace"))
        return paths, w, h, note
    if ext == ".svg":
        text = src.read_text(errors="replace")
    elif ext in (".pdf", ".ai"):
        text = _pdf_to_svg(src)
    elif ext in (".eps", ".ps"):
        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / "in.pdf"
            _ps_to_pdf(src, pdf)
            text = _pdf_to_svg(pdf)
    else:
        raise ArtworkError(f"unsupported file type {ext}")

    unitless = "px" if ext == ".svg" else "pt"   # pdftocairo writes points without a unit
    paths, w, h = svg_to_paths(text, cut_filter, unitless)
    if cut_filter and not paths:
        # PDFs lose spot colour names on conversion; fall back to a colour match
        # done by the caller, or cut everything with a warning.
        paths, w, h = svg_to_paths(text, None, unitless)
        note = ("No CutContour line found, so every vector line is shown. "
                "Check the preview before cutting.")
    return paths, w, h, note
