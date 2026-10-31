"""Lay the artwork on a print sheet with registration marks and a job QR code.

Produces print.pdf (for the Xerox) and the cut paths in the same sheet
coordinates, so the cutter can cut relative to the marks.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf._page import PageObject
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from . import strip
from .geometry import ArtworkError

GAP_MM = 3.0


def artwork_as_pdf(src: Path, workdir: Path, cut_filter=None):
    """Return (pdf, removed): a PDF of the artwork for printing, converted
    from SVG/EPS if needed, with the cut lines taken out when cut_filter is
    given, and how many cut lines were removed."""
    pdf = _convert(src, workdir, cut_filter)
    if cut_filter is None:
        return pdf, 0
    out = workdir / "artwork-print.pdf"
    removed = strip.strip_pdf(pdf, out, cut_filter)
    return out, removed + getattr(_convert, "svg_removed", 0)


def _convert(src: Path, workdir: Path, cut_filter=None) -> Path:
    ext = src.suffix.lower()
    _convert.svg_removed = 0
    if ext in (".pdf", ".ai"):
        return src
    out = workdir / "artwork-converted.pdf"
    if ext == ".svg":
        if cut_filter is not None:
            clean = workdir / "artwork-print.svg"
            _convert.svg_removed = strip.strip_svg(src, clean, cut_filter)
            src = clean
        if shutil.which("rsvg-convert"):
            cmd = ["rsvg-convert", "-f", "pdf", "-o", str(out), str(src)]
        elif shutil.which("inkscape"):
            cmd = ["inkscape", str(src), "--export-type=pdf", f"--export-filename={out}"]
        else:
            raise ArtworkError("SVG printing needs rsvg-convert (sudo apt install librsvg2-bin)")
    elif ext in (".eps", ".ps"):
        if not shutil.which("ps2pdf"):
            raise ArtworkError("EPS printing needs ghostscript (sudo apt install ghostscript)")
        cmd = ["ps2pdf", "-dEPSCrop", str(src), str(out)]
    else:
        raise ArtworkError(f"{ext} files can be cut but not printed")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not out.exists():
        raise ArtworkError(f"could not convert artwork to PDF: {r.stderr.strip()[:200]}")
    return out


def mark_centres(cfg, W=None, H=None):
    """Mark centres. mark_inset_mm is the gap from the sheet edge to the
    mark's outer edge, as in SignCut ("5 mm marks, 10 mm from the edge")."""
    W, H = W or cfg.sheet_width_mm, H or cfg.sheet_height_mm
    i = cfg.mark_inset_mm + cfg.mark_size_mm / 2
    marks = [(i, i), (W - i, i), (i, H - i)]
    if cfg.mark_count >= 4:
        marks.append((W - i, H - i))
    return marks


def cut_origin(cfg, W=None, H=None):
    """Where cut coordinate 0,0 is on the sheet. SignCut's QR jobs count
    from the centre of the first (bottom-left) mark."""
    x, y = mark_centres(cfg, W, H)[0]
    if cfg.origin in ("mark1", "mark1centre"):
        return x, y
    if cfg.origin == "mark1corner":
        return x - cfg.mark_size_mm / 2, y - cfg.mark_size_mm / 2
    return 0.0, 0.0


def qr_position(cfg, W=None, H=None):
    """Bottom-left corner of the QR code: next to mark 1 on the bottom edge."""
    mx, my = mark_centres(cfg, W, H)[0]
    return mx + cfg.mark_size_mm / 2 + GAP_MM * 2, my - cfg.qr_size_mm / 2


def print_area(cfg, W=None, H=None):
    """Rectangle (x0, y0, x1, y1) in mm where artwork may go."""
    i, s = cfg.mark_inset_mm + cfg.mark_size_mm / 2, cfg.mark_size_mm
    side = i + s / 2 + GAP_MM
    bottom = i + max(s, cfg.qr_size_mm) / 2 + GAP_MM
    return side, bottom, (W or cfg.sheet_width_mm) - side, (H or cfg.sheet_height_mm) - side


def _overlay(cfg, job_id, qr_payload, path: Path, W_mm, H_mm):
    W, H = W_mm * mm, H_mm * mm
    c = canvas.Canvas(str(path), pagesize=(W, H))
    s = cfg.mark_size_mm
    c.setFillColorRGB(0, 0, 0)
    for x, y in mark_centres(cfg, W_mm, H_mm):
        if cfg.mark_shape == "square":
            c.rect((x - s / 2) * mm, (y - s / 2) * mm, s * mm, s * mm, stroke=0, fill=1)
        else:
            c.circle(x * mm, y * mm, s / 2 * mm, stroke=0, fill=1)

    q = cfg.qr_size_mm
    qx, qy = qr_position(cfg, W_mm, H_mm)
    w = QrCodeWidget(qr_payload, barLevel="M")
    x0, y0, x1, y1 = w.getBounds()
    d = Drawing(q * mm, q * mm, transform=[q * mm / (x1 - x0), 0, 0, q * mm / (y1 - y0), 0, 0])
    d.add(w)
    renderPDF.draw(d, c, qx * mm, qy * mm)
    c.setFont("Helvetica", 8)
    c.drawString((qx + q + 2) * mm, (qy + q / 2 - 1) * mm, job_id)
    c.showPage()
    c.save()


def build_print_sheet(cfg, job_id, art_pdf: Path, out_pdf: Path):
    """Write out_pdf. Returns (offset_x_mm, offset_y_mm, sheet_w_mm, sheet_h_mm):
    where the artwork's bottom-left corner sits on the sheet, and the sheet size.

    Artwork that fits inside the marks on the configured sheet is centred on
    it. Artwork that doesn't (e.g. an A3 page laid out for print and cut,
    the way SignCut works) becomes the sheet itself, and the marks and QR go
    on top of it near its edges."""
    reader = PdfReader(str(art_pdf))
    if not reader.pages:
        raise ArtworkError("artwork PDF has no pages")
    art = reader.pages[0]
    box = art.cropbox
    aw = float(box.width) / mm
    ah = float(box.height) / mm
    x0, y0, x1, y1 = print_area(cfg)
    if aw <= x1 - x0 + 0.01 and ah <= y1 - y0 + 0.01:
        W, H = cfg.sheet_width_mm, cfg.sheet_height_mm
        ox = x0 + ((x1 - x0) - aw) / 2
        oy = y0 + ((y1 - y0) - ah) / 2
    else:
        W, H, ox, oy = aw, ah, 0.0, 0.0
        if not (185 <= min(W, H) and max(W, H) <= 500 and min(W, H) <= 350):
            raise ArtworkError(f"artwork page is {aw:.0f} x {ah:.0f} mm; the SC-350 takes sheets "
                               f"185-350 mm wide and 150-500 mm long")

    with tempfile.TemporaryDirectory() as td:
        ov_path = Path(td) / "overlay.pdf"
        payload = cfg.qr_text.format(job_id=job_id)
        _overlay(cfg, job_id, payload, ov_path, W, H)
        overlay = PdfReader(str(ov_path)).pages[0]

        sheet = PageObject.create_blank_page(width=W * mm, height=H * mm)
        t = Transformation().translate(ox * mm - float(box.left), oy * mm - float(box.bottom))
        sheet.merge_transformed_page(art, t)
        sheet.merge_page(overlay)
        w = PdfWriter()
        w.add_page(sheet)
        w.add_metadata({"/Title": job_id, "/Subject": "SC-350 print and cut"})
        with open(out_pdf, "wb") as f:
            w.write(f)
    return ox, oy, W, H
