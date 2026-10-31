"""Job store: hot folder intake, active cut queue, archive, repeat orders.

Each job is a folder holding job.json, the artwork, print.pdf (artwork
with marks + QR), cut.plt and preview.svg. Jobs live in the queue folder
for cfg.queue_hours, then move to archive/YYYY/MM/<job id>/.
"""
import base64
import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import cutter, geometry, marks

STABLE_SECONDS = 3       # a hot folder file must stop growing this long


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256(path: Path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def normal_name(name: str):
    stem = Path(name).stem.lower()
    stem = re.sub(r"\s*\(\d+\)$", "", stem)          # "label (1)"
    stem = re.sub(r"[\s_-]*copy$", "", stem)          # "label copy"
    stem = re.sub(r"[\s_\-.]+", " ", stem).strip()
    return stem


def _render_png(pdf: Path):
    """Low-res picture of the print sheet for the preview, or None."""
    if not pdf.exists() or not shutil.which("pdftoppm"):
        return None
    r = subprocess.run(["pdftoppm", "-r", "60", "-png", "-singlefile", str(pdf)],
                       capture_output=True, timeout=60)
    return base64.b64encode(r.stdout).decode() if r.returncode == 0 and r.stdout else None


class Store:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.lock = threading.RLock()
        for d in (cfg.hotfolder, cfg.queue, cfg.archive, cfg.plt_export):
            d.mkdir(parents=True, exist_ok=True)
        self.rejected = cfg.hotfolder / "_rejected"
        self.rejected.mkdir(exist_ok=True)
        self.jobs = {}      # id -> folder
        self.rescan()
        self._seen = {}     # hot folder path -> (size, mtime, first_seen)

    # ------------------------------------------------------------------ index

    def rescan(self):
        with self.lock:
            self.jobs = {}
            for root in (self.cfg.queue, self.cfg.archive):
                for j in root.rglob("job.json"):
                    try:
                        jid = json.loads(j.read_text())["id"]
                        self.jobs[jid] = j.parent
                    except (ValueError, KeyError, OSError):
                        continue

    def load(self, jid):
        folder = self.jobs.get(jid)
        if not folder:
            return None
        meta = json.loads((folder / "job.json").read_text())
        meta["folder"] = str(folder)
        return meta

    def save(self, meta):
        folder = Path(meta["folder"])
        data = {k: v for k, v in meta.items() if k != "folder"}
        tmp = folder / "job.json.tmp"
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(folder / "job.json")

    def all(self):
        with self.lock:
            out = []
            for jid in list(self.jobs):
                try:
                    out.append(self.load(jid))
                except (OSError, ValueError):
                    continue
            return out

    def find(self, text):
        """Look a job up by what a QR scan or a search box gives us."""
        text = text.strip()
        if not text:
            return []
        with self.lock:
            if text in self.jobs:
                return [self.load(text)]
            hits = []
            low = text.lower()
            for meta in self.all():
                qr = self.qr_text(meta["id"])
                if low in (meta["id"].lower(), qr.lower()) or low in meta["name"].lower() \
                        or low in (meta.get("customer") or "").lower():
                    hits.append(meta)
            return hits

    def qr_text(self, jid):
        return self.cfg.qr_text.format(job_id=jid)

    def _new_id(self):
        day = datetime.now().strftime("%y%m%d")
        prefix = f"{self.cfg.job_prefix}-{day}-"
        n = max([int(j[len(prefix):]) for j in self.jobs if j.startswith(prefix)
                 and j[len(prefix):].isdigit()] or [0])
        return f"{prefix}{n + 1:03d}"

    # ------------------------------------------------------------------ hot folder

    def poll_hotfolder(self):
        hf = self.cfg.hotfolder
        present = set()
        for p in sorted(hf.iterdir()):
            if p.is_dir() or p.name.startswith((".", "~")) or p.suffix.lower() in (".tmp", ".part", ".crdownload"):
                continue
            present.add(p)
            st = p.stat()
            key = (st.st_size, st.st_mtime)
            prev = self._seen.get(p)
            if not prev or prev[0] != key:
                self._seen[p] = (key, time.time())
                continue
            if time.time() - prev[1] < STABLE_SECONDS:
                continue
            del self._seen[p]
            self.ingest(p)
        for p in list(self._seen):
            if p not in present:
                del self._seen[p]

    def _reject(self, src: Path, reason: str):
        dst = self.rejected / src.name
        shutil.move(str(src), dst)
        (self.rejected / (src.name + ".txt")).write_text(reason + "\n")
        self.log(f"rejected {src.name}: {reason}")

    def ingest(self, src: Path):
        if src.suffix.lower() not in geometry.SUPPORTED:
            self._reject(src, f"{src.suffix or 'this'} files are not supported. "
                              f"Use {', '.join(sorted(geometry.SUPPORTED))}")
            return None
        digest = sha256(src)
        with self.lock:
            metas = self.all()
            same = [m for m in metas if m.get("hash") == digest]
            if same:
                meta = same[0]
                src.unlink()
                self.requeue(meta["id"], why="repeat order: same artwork dropped again")
                self.log(f"{src.name}: repeat of {meta['id']}, back in the queue")
                return meta["id"]
            if self.cfg.reuse_by_filename:
                key = normal_name(src.name)
                named = [m for m in metas if normal_name(m["name"]) == key]
                if named:
                    meta = max(named, key=lambda m: m.get("revision", 1))
                    return self._new_revision(meta, src, digest)
            jid = self._new_id()
            folder = self.cfg.queue / jid
            folder.mkdir(parents=True)
            art = folder / ("artwork" + src.suffix.lower())
            shutil.move(str(src), art)
            meta = {"id": jid, "name": src.name, "hash": digest, "revision": 1,
                    "artwork": art.name, "created": now_iso(), "queued_at": now_iso(),
                    "status": "queued", "prints": 0, "cuts": 0, "customer": "",
                    "history": [[now_iso(), f"new job from {src.name}"]],
                    "folder": str(folder)}
            self.jobs[jid] = folder
            self.save(meta)
        self.prepare(jid)
        return jid

    def _new_revision(self, meta, src, digest):
        jid = meta["id"]
        folder = self._move_to_queue(meta)
        rev = meta.get("revision", 1)
        old = folder / "versions" / f"r{rev}"
        old.mkdir(parents=True, exist_ok=True)
        for f in folder.iterdir():
            if f.is_file() and f.name != "job.json":
                shutil.move(str(f), old / f.name)
        art = folder / ("artwork" + src.suffix.lower())
        shutil.move(str(src), art)
        meta.update(folder=str(folder), name=src.name, hash=digest, revision=rev + 1,
                    artwork=art.name, queued_at=now_iso(), status="queued", error="", note="",
                    printed_at="", cut_at="")
        meta["history"].append([now_iso(), f"updated artwork {src.name} (revision {rev + 1})"])
        self.save(meta)
        self.log(f"{src.name}: new revision of {jid}")
        self.prepare(jid)
        return jid

    # ------------------------------------------------------------------ build files

    def prepare(self, jid):
        """Read the artwork, make print.pdf, cut.plt and preview.svg."""
        meta = self.load(jid)
        folder = Path(meta["folder"])
        art = folder / meta["artwork"]
        cfg = self.cfg
        cf = geometry.CutFilter(cfg.cut_names, cfg.cut_colors)
        try:
            paths, aw, ah, note = geometry.load_artwork(art, cf)
            if not paths:
                raise geometry.ArtworkError("no cut lines found in the artwork")
            sheet_paths = paths
            sheet = None
            if art.suffix.lower() in (".plt", ".hpgl", ".hpg"):
                origin = (0.0, 0.0)
                meta["printable"] = False
            else:
                # a file with no named cut line is cut along all its lines;
                # keep those in the print, there is nothing to hide
                pdf, hidden = marks.artwork_as_pdf(art, folder, None if note else cf)
                meta["cut_lines_hidden"] = hidden
                ox, oy, sw, sh = marks.build_print_sheet(cfg, jid, pdf, folder / "print.pdf")
                meta["sheet_mm"] = [round(sw, 2), round(sh, 2)]
                sheet = (sw, sh)
                sheet_paths = [[(x + ox, y + oy) for x, y in p] for p in paths]
                origin = marks.cut_origin(cfg, sw, sh)
                meta["printable"] = True
                meta["offset_mm"] = [round(ox, 2), round(oy, 2)]
            (folder / "cut.json").write_text(json.dumps(sheet_paths))
            cut_paths, cut_origin, cut_sheet = sheet_paths, origin, sheet
            if sheet and sheet[0] < sheet[1] and cfg.rotate_portrait:
                # the cutter's X axis runs along the feed (long side, max 500 mm)
                # and Y across it (max 350 mm), so a portrait sheet goes in turned
                cut_paths, cut_origin, cut_sheet = cutter.rotate(sheet_paths, origin, sheet)
            plt = cutter.to_hpgl(cutter.transform(cut_paths, cfg, cut_origin), cfg, cut_sheet)
            (folder / "cut.plt").write_text(plt)
            cutter.export(plt, cfg.plt_export, self.qr_text(jid))
            (folder / "preview.svg").write_text(self.preview_svg(meta, sheet_paths, folder / "print.pdf"))
            meta.update(error="", note=note, size_mm=[round(aw, 1), round(ah, 1)],
                        cut_paths=len(paths),
                        cut_length_mm=round(sum(sum(((p[i][0] - p[i - 1][0]) ** 2 + (p[i][1] - p[i - 1][1]) ** 2) ** .5
                                                    for i in range(1, len(p))) for p in paths)))
            self.log(f"{jid}: ready, {len(paths)} cut paths")
        except (geometry.ArtworkError, OSError, ValueError) as e:
            meta["error"] = str(e)
            self.log(f"{jid}: {e}")
        self.save(meta)
        return meta

    def preview_svg(self, meta, sheet_paths, print_pdf=None):
        cfg = self.cfg
        W, H = (meta.get("sheet_mm") or (cfg.sheet_width_mm, cfg.sheet_height_mm)) if meta.get("printable") else (
            max([x for p in sheet_paths for x, _ in p] + [10]), max([y for p in sheet_paths for _, y in p] + [10]))
        parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.2f} {H:.2f}" '
                 f'width="{W:.2f}mm" height="{H:.2f}mm">',
                 f'<rect width="{W:.2f}" height="{H:.2f}" fill="#fff" stroke="#999" stroke-width="0.5"/>']
        png = _render_png(print_pdf) if meta.get("printable") and print_pdf else None
        if png:
            parts.append(f'<image href="data:image/png;base64,{png}" width="{W:.2f}" height="{H:.2f}"/>')
        elif meta.get("printable"):
            s = cfg.mark_size_mm
            for x, y in marks.mark_centres(cfg, W, H):
                parts.append(f'<circle cx="{x:.2f}" cy="{H - y:.2f}" r="{s / 2}" fill="#000"/>')
            q = cfg.qr_size_mm
            qx, qy = marks.qr_position(cfg, W, H)
            parts.append(f'<rect x="{qx:.2f}" y="{H - qy - q:.2f}" width="{q}" height="{q}" '
                         f'fill="none" stroke="#000" stroke-width="0.4" stroke-dasharray="1 1"/>')
            parts.append(f'<text x="{marks.qr_label_x(cfg, meta["id"], W, H):.2f}" y="{H - qy - q / 2 + 1:.2f}" font-size="3" '
                         f'font-family="sans-serif">QR {meta["id"]}</text>')
        for p in sheet_paths:
            d = "M" + " L".join(f"{x:.2f},{H - y:.2f}" for x, y in p)
            parts.append(f'<path d="{d}" fill="none" stroke="#e6007e" stroke-width="0.4"/>')
        parts.append("</svg>")
        return "\n".join(parts)

    # ------------------------------------------------------------------ queue / archive

    def _move_to_queue(self, meta):
        folder = Path(meta["folder"])
        if folder.parent == self.cfg.queue:
            return folder
        dst = self.cfg.queue / meta["id"]
        shutil.move(str(folder), dst)
        self.jobs[meta["id"]] = dst
        meta["folder"] = str(dst)
        return dst

    def requeue(self, jid, why="sent back to the cut queue"):
        with self.lock:
            meta = self.load(jid)
            if not meta:
                raise KeyError(jid)
            self._move_to_queue(meta)
            # a new order: clear the printed / cut ticks of the last one
            meta.update(status="queued", queued_at=now_iso(), printed_at="", cut_at="")
            meta["history"].append([now_iso(), why])
            self.save(meta)
            # keep the PLT the cutter loads by QR in place
            plt = Path(meta["folder"]) / "cut.plt"
            if plt.exists():
                shutil.copy(plt, self.cfg.plt_export / f"{self.qr_text(jid)}.plt")
            return meta

    def archive(self, jid, why="moved to archive"):
        with self.lock:
            meta = self.load(jid)
            if not meta or meta["status"] == "archived":
                return meta
            when = datetime.now()
            dst = self.cfg.archive / when.strftime("%Y") / when.strftime("%m") / jid
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(meta["folder"], dst)
            self.jobs[jid] = dst
            meta.update(folder=str(dst), status="archived", archived_at=now_iso())
            meta["history"].append([now_iso(), why])
            self.save(meta)
            self.log(f"{jid}: archived")
            return meta

    def archive_due(self):
        limit = self.cfg.queue_hours * 3600
        for meta in self.all():
            if meta["status"] != "queued":
                continue
            age = time.time() - datetime.fromisoformat(meta["queued_at"]).timestamp()
            if age >= limit:
                self.archive(meta["id"], why=f"archived after {self.cfg.queue_hours:g} hours in the queue")

    def delete(self, jid):
        with self.lock:
            meta = self.load(jid)
            if not meta:
                return
            shutil.rmtree(meta["folder"])
            (self.cfg.plt_export / f"{self.qr_text(jid)}.plt").unlink(missing_ok=True)
            del self.jobs[jid]

    def note(self, jid, text, **fields):
        with self.lock:
            meta = self.load(jid)
            meta.update(fields)
            meta["history"].append([now_iso(), text])
            self.save(meta)
            return meta
