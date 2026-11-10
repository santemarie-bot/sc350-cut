"""JJB jar wrap labels: contour cut 0.5 mm inside the art edge, art stretched
2.5 mm past the cut (bleed), ganged rotated on a page that fits the A3 print area."""
import sys, numpy as np, cv2
from pathlib import Path
from PIL import Image
from scipy import ndimage as ndi
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.colors import CMYKColorSep
from reportlab.lib.utils import ImageReader

HEIGHT_MM = 45.0      # overall cut height incl. the cap
INSET_MM = 0.5        # cut this far inside the art edge (no white on the edge)
BLEED_MM = 2.5        # art stretched past the cut
GAP_MM = 6.0          # between cut lines
AREA = (261.0, 381.5)  # print area inside the marks on A3 (sc350cut print_area)


def prepare(path):
    a = np.asarray(Image.open(path).convert("RGB"))
    H, W = a.shape[:2]
    m = a.min(axis=2) < 235
    lab, n = ndi.label(m)
    m = lab == (np.bincount(lab.ravel())[1:].argmax() + 1)
    m = ndi.binary_fill_holes(m)
    rows = np.where(m.any(1))[0]
    hpx = rows.max() + 1 - rows.min()
    pxmm = hpx / (HEIGHT_MM + 2 * INSET_MM)  # art height = cut height + 2 insets
    inset, bleed = INSET_MM * pxmm, BLEED_MM * pxmm
    P = int(bleed) + 8
    mp = np.pad(m, P)  # outside the picture counts as white
    # cut mask: art eroded by the inset, smoothed
    dist_in = ndi.distance_transform_edt(mp)
    cutm = dist_in > inset
    cutm = cv2.GaussianBlur(cutm.astype(np.float32), (0, 0), 2.0) > 0.5
    cs, _ = cv2.findContours(cutm.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    c = max(cs, key=cv2.contourArea)
    c = cv2.approxPolyDP(c, 0.6, True)[:, 0, :].astype(float)
    # recompute the cut mask from the polygon, then the bleed area around it
    poly = np.zeros(mp.shape, np.uint8)
    cv2.fillPoly(poly, [c.astype(np.int32)], 1)
    bleedm = ndi.distance_transform_edt(poly == 0) <= bleed
    # colours: every pixel takes the colour of the nearest solid art pixel
    core = dist_in > 3
    ap = np.pad(a, ((P, P), (P, P), (0, 0)))
    _, (iy, ix) = ndi.distance_transform_edt(~core, return_indices=True)
    out = ap.copy()
    fill = ~core
    out[fill] = ap[iy[fill], ix[fill]]
    out[~bleedm] = 255
    # crop to the bleed box
    ys, xs = np.where(bleedm)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    out = out[y0:y1, x0:x1]
    c = c - [x0, y0]
    return Image.fromarray(out), c, pxmm


def rot_ccw(img, pts):
    W = img.width
    return img.transpose(Image.Transpose.ROTATE_90), np.c_[pts[:, 1], W - pts[:, 0]]


def sheet(items, out_pdf, title):
    """items: list of (img, pts, pxmm) already rotated, all ~same size."""
    w = max(i[0].width / i[2] for i in items)
    h = max(i[0].height / i[2] for i in items)
    b = BLEED_MM  # boxes include the bleed; step = cut width + gap
    step_x, step_y = w - 2 * b + GAP_MM, h - 2 * b + GAP_MM
    cols = int((AREA[0] - w) // step_x) + 1
    rows = int((AREA[1] - h) // step_y) + 1
    n = min(len(items), cols * rows)
    cols = min(cols, n)
    rows = -(-n // cols)
    PW, PH = w + (cols - 1) * step_x, h + (rows - 1) * step_y
    cv = canvas.Canvas(str(out_pdf), pagesize=(PW * mm, PH * mm))
    cv.setTitle(title)
    cuts = []
    for k, (img, pts, pxmm) in enumerate(items[:n]):
        r, q = divmod(k, cols)
        X, Y = q * step_x, PH - h - r * step_y
        iw, ih = img.width / pxmm, img.height / pxmm
        cv.drawImage(ImageReader(img), X * mm, Y * mm, iw * mm, ih * mm)
        cuts.append([(X + x / pxmm, Y + ih - y / pxmm) for x, y in pts])
    cv.setStrokeColor(CMYKColorSep(0, 1, 0, 0, spotName="CutContour"))
    cv.setLineWidth(0.25)
    for p in cuts:
        path = cv.beginPath()
        path.moveTo(p[0][0] * mm, p[0][1] * mm)
        for x, y in p[1:]:
            path.lineTo(x * mm, y * mm)
        path.close()
        cv.drawPath(path, stroke=1, fill=0)
    cv.showPage()
    cv.save()
    return n, PW, PH


if __name__ == "__main__":
    outdir = Path(sys.argv[1])
    outdir.mkdir(parents=True, exist_ok=True)
    labels = {}
    for name, f in zip(sys.argv[2::2], sys.argv[3::2]):
        img, pts, pxmm = prepare(f)
        cw = (pts[:, 0].max() - pts[:, 0].min()) / pxmm
        ch = (pts[:, 1].max() - pts[:, 1].min()) / pxmm
        print(f"{name}: cut {cw:.1f} x {ch:.1f} mm, {pxmm * 25.4:.0f} dpi, {len(pts)} points")
        img.save(outdir / f"{name}-label.png", dpi=(pxmm * 25.4,) * 2)
        labels[name] = rot_ccw(img, pts) + (pxmm,)
    for name, it in labels.items():
        n, PW, PH = sheet([it] * 20, outdir / f"JJB-{name}.pdf", f"JJB {name}")
        print(f"JJB-{name}.pdf: {n} labels, page {PW:.1f} x {PH:.1f} mm")
