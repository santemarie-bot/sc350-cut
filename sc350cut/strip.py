"""Remove the cut lines from artwork before it is printed.

Print-and-cut files carry the picture and the CutContour line together.
The cutter needs the line; the printer must not print it.

PDF (and EPS/AI, which arrive as PDF): every path painted in a cut colour
is dropped from the page's drawing instructions, including inside nested
forms, which Illustrator uses a lot. A cut colour is a spot colour whose
name matches the cut names (CutContour, ...) or a plain RGB/CMYK colour
that matches the configured cut colours.

SVG: elements the cut filter matches are removed before conversion.
"""
import xml.etree.ElementTree as ET
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, ContentStream, NameObject

from . import geometry

PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
STROKES = {"S", "s", "B", "B*", "b", "b*"}
FILLS = {"f", "F", "f*", "B", "B*", "b", "b*"}


def _rgb(vals):
    return tuple(round(float(v) * 255) for v in vals)


def _cmyk_to_rgb(c, m, y, k):
    c, m, y, k = (float(v) for v in (c, m, y, k))
    return tuple(round(255 * (1 - x) * (1 - k)) for x in (c, m, y))


class _Colors:
    def __init__(self, cut_filter):
        self.f = cut_filter

    def rgb_is_cut(self, rgb):
        return any(max(abs(a - b) for a, b in zip(rgb, k)) <= self.f.tolerance for k in self.f.colors)

    def name_is_cut(self, name):
        return geometry.name_hit(self.f.names, str(name).lstrip("/"))


def _spot_names(resources):
    """Colour space resource names that are a cut spot colour, e.g. {'/CS0': 'CutContour'}."""
    out = {}
    if not resources or "/ColorSpace" not in resources:
        return out
    for key, cs in resources["/ColorSpace"].get_object().items():
        cs = cs.get_object()
        if isinstance(cs, ArrayObject) and len(cs) >= 2 and str(cs[0]) == "/Separation":
            out[key] = str(cs[1]).lstrip("/")
        elif isinstance(cs, ArrayObject) and len(cs) >= 2 and str(cs[0]) == "/DeviceN":
            out[key] = " ".join(str(n).lstrip("/") for n in cs[1].get_object())
    return out


def _clean(stream_owner, resources, reader, colors, depth=0):
    """Rewrite one content stream in place. Returns number of paths removed."""
    spots = _spot_names(resources)
    content = ContentStream(stream_owner, reader) if depth else stream_owner
    # state: (stroke_is_cut, fill_is_cut, stroke_space, fill_space)
    state = [False, False, None, None]
    stack = []
    removed = 0
    ops = []
    for operands, op in content.operations:
        o = op.decode("latin-1") if isinstance(op, bytes) else op
        if o == "q":
            stack.append(list(state))
        elif o == "Q" and stack:
            state = stack.pop()
        elif o == "CS":
            state[2] = str(operands[0])
            state[0] = state[2] in spots and colors.name_is_cut(spots[state[2]])
        elif o == "cs":
            state[3] = str(operands[0])
            state[1] = state[3] in spots and colors.name_is_cut(spots[state[3]])
        elif o in ("SC", "SCN"):
            if state[2] in spots:
                state[0] = colors.name_is_cut(spots[state[2]])
            elif len(operands) == 3:
                state[0] = colors.rgb_is_cut(_rgb(operands))
            elif len(operands) == 4:
                state[0] = colors.rgb_is_cut(_cmyk_to_rgb(*operands))
        elif o in ("sc", "scn"):
            if state[3] in spots:
                state[1] = colors.name_is_cut(spots[state[3]])
            elif len(operands) == 3:
                state[1] = colors.rgb_is_cut(_rgb(operands))
            elif len(operands) == 4:
                state[1] = colors.rgb_is_cut(_cmyk_to_rgb(*operands))
        elif o == "RG":
            state[0] = colors.rgb_is_cut(_rgb(operands))
        elif o == "rg":
            state[1] = colors.rgb_is_cut(_rgb(operands))
        elif o == "K":
            state[0] = colors.rgb_is_cut(_cmyk_to_rgb(*operands))
        elif o == "k":
            state[1] = colors.rgb_is_cut(_cmyk_to_rgb(*operands))
        elif o in ("G", "g"):
            state[0 if o == "G" else 1] = False
        elif o in PAINT:
            stroke_cut = o in STROKES and state[0]
            fill_cut = o in FILLS and state[1]
            if stroke_cut or fill_cut:
                removed += 1
                keep_fill = o in FILLS and not fill_cut
                keep_stroke = o in STROKES and not stroke_cut
                even_odd = "*" in o
                if keep_fill:
                    op = b"f*" if even_odd else b"f"
                elif keep_stroke:
                    op = b"S"
                else:
                    op = b"n"
        elif o == "Do" and depth < 10 and resources and "/XObject" in resources:
            xo = resources["/XObject"].get_object().get(operands[0])
            if xo is not None:
                xobj = xo.get_object()
                if xobj.get("/Subtype") == "/Form":
                    sub_res = xobj.get("/Resources")
                    sub_res = sub_res.get_object() if sub_res is not None else resources
                    removed += _clean(xobj, sub_res, reader, colors, depth + 1)
        ops.append((operands, op))
    content.operations = ops
    if depth:
        # pypdf only re-encodes Flate streams, so store the form as Flate
        # whatever it was encoded with before (ASCII85, LZW, ...)
        if "/Filter" in stream_owner:
            stream_owner.get_data()
            stream_owner[NameObject("/Filter")] = NameObject("/FlateDecode")
            if "/DecodeParms" in stream_owner:
                del stream_owner["/DecodeParms"]
        stream_owner.set_data(content.get_data())
    return removed


def strip_pdf(src: Path, dst: Path, cut_filter) -> int:
    """Write dst = src without cut lines. Returns how many paths were removed."""
    try:
        return _strip_pdf(src, dst, cut_filter)
    except Exception as e:  # never print cut lines silently
        raise geometry.ArtworkError(f"could not take the cut lines out of the print: {e}")


def _strip_pdf(src, dst, cut_filter):
    reader = PdfReader(str(src))
    writer = PdfWriter(clone_from=reader)
    colors = _Colors(cut_filter)
    removed = 0
    for page in writer.pages:
        res = page.get("/Resources")
        res = res.get_object() if res is not None else None
        content = page.get_contents()
        if content is None:
            continue
        removed += _clean(content, res, writer, colors)
        page.replace_contents(content)
    with open(dst, "wb") as f:
        writer.write(f)
    return removed


def strip_svg(src: Path, dst: Path, cut_filter) -> int:
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    ET.register_namespace("inkscape", "http://www.inkscape.org/namespaces/inkscape")
    tree = ET.parse(src)
    root = tree.getroot()
    removed = 0

    def walk(el, st, layers):
        nonlocal removed
        for ch in list(el):
            tag = geometry._local(ch.tag)
            cst = geometry._style(ch, st)
            label = ch.get("{http://www.inkscape.org/namespaces/inkscape}label") or ch.get("id") or ""
            if tag == "g":
                if label and cut_filter.matches(ch, cst, [label]):
                    el.remove(ch)
                    removed += 1
                    continue
                walk(ch, cst, layers + [label])
            elif tag in ("path", "rect", "circle", "ellipse", "line", "polyline", "polygon"):
                if cut_filter.matches(ch, cst, layers):
                    el.remove(ch)
                    removed += 1

    walk(root, {}, [])
    tree.write(dst)
    return removed
