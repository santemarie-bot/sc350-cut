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

from .geometry import ArtworkError

GAP_MM = 3.0


def artwork_as_pdf(src: Path, workdir: Path) -> Path:
    """Return a PDF of the artwork, converting SVG/EPS if needed."""
    ext = src.suffix.lower()
    if ext in (".pdf", ".ai"):
        return src
    out = workdir / "artwork-converted.pdf"
    if ext == ".svg":
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


def mark_centres(cfg):
    W, H, i = cfg.sheet_width_mm, cfg.sheet_height_mm, cfg.mark_inset_mm
    marks = [(i, i), (W - i, i), (i, H - i)]
    if cfg.mark_count >= 4:
        marks.append((W - i, H - i))
    return marks


def print_area(cfg):
    """Rectangle (x0, y0, x1, y1) in mm where artwork may go."""
    i, s = cfg.mark_inset_mm, cfg.mark_size_mm
    side = i + s / 2 + GAP_MM
    bottom = i + max(s, cfg.qr_size_mm) / 2 + GAP_MM
    return side, bottom, cfg.sheet_width_mm - side, cfg.sheet_height_mm - side


def _overlay(cfg, job_id, qr_payload, path: Path):
    W, H = cfg.sheet_width_mm * mm, cfg.sheet_height_mm * mm
    c = canvas.Canvas(str(path), pagesize=(W, H))
    s = cfg.mark_size_mm
    c.setFillColorRGB(0, 0, 0)
    for x, y in mark_centres(cfg):
        c.rect((x - s / 2) * mm, (y - s / 2) * mm, s * mm, s * mm, stroke=0, fill=1)

    q = cfg.qr_size_mm
    qx = cfg.mark_inset_mm + s / 2 + GAP_MM * 2
    qy = cfg.mark_inset_mm - q / 2
    w = QrCodeWidget(qr_payload, barLevel="M")
    x0, y0, x1, y1 = w.getBounds()
    d = Drawing(q * mm, q * mm, transform=[q * mm / (x1 - x0), 0, 0, q * mm / (y1 - y0), 0, 0])
    d.add(w)
    renderPDF.draw(d, c, qx * mm, qy * mm)
    c.setFont("Helvetica", 8)
    c.drawString((qx + q + 2) * mm, (cfg.mark_inset_mm - 1) * mm, job_id)
    c.showPage()
    c.save()


def build_print_sheet(cfg, job_id, art_pdf: Path, out_pdf: Path, art_w_mm=None, art_h_mm=None):
    """Write out_pdf. Returns (offset_x_mm, offset_y_mm) where the artwork's
    bottom-left corner sits on the sheet."""
    reader = PdfReader(str(art_pdf))
    if not reader.pages:
        raise ArtworkError("artwork PDF has no pages")
    art = reader.pages[0]
    box = art.cropbox
    aw = float(box.width) / mm
    ah = float(box.height) / mm
    x0, y0, x1, y1 = print_area(cfg)
    if aw > x1 - x0 + 0.01 or ah > y1 - y0 + 0.01:
        raise ArtworkError(
            f"artwork is {aw:.0f} x {ah:.0f} mm but only {x1 - x0:.0f} x {y1 - y0:.0f} mm "
            f"fits inside the marks on a {cfg.sheet_width_mm:.0f} x {cfg.sheet_height_mm:.0f} mm sheet")
    ox = x0 + ((x1 - x0) - aw) / 2
    oy = y0 + ((y1 - y0) - ah) / 2

    with tempfile.TemporaryDirectory() as td:
        ov_path = Path(td) / "overlay.pdf"
        payload = cfg.qr_text.format(job_id=job_id)
        _overlay(cfg, job_id, payload, ov_path)
        overlay = PdfReader(str(ov_path)).pages[0]

        sheet = PageObject.create_blank_page(width=cfg.sheet_width_mm * mm, height=cfg.sheet_height_mm * mm)
        t = Transformation().translate(ox * mm - float(box.left), oy * mm - float(box.bottom))
        sheet.merge_transformed_page(art, t)
        sheet.merge_page(overlay)
        w = PdfWriter()
        w.add_page(sheet)
        w.add_metadata({"/Title": job_id, "/Subject": "SC-350 print and cut"})
        with open(out_pdf, "wb") as f:
            w.write(f)
    return ox, oy
