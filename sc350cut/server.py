"""Web page + background workers (hot folder watcher, 24 h archiver)."""
import json
import mimetypes
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import cutter
from .jobs import Store, now_iso

UI = Path(__file__).with_name("ui.html")
FILES = {"preview.svg", "print.pdf", "cut.plt"}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def worker(store: Store):
    last_archive = 0
    while True:
        try:
            store.poll_hotfolder()
            if time.time() - last_archive > 60:
                store.archive_due()
                last_archive = time.time()
        except Exception:
            log(traceback.format_exc())
        time.sleep(1)


TRAYS = {"5": "manual", "1": "tray-1", "2": "tray-2", "3": "tray-3", "4": "tray-4"}


def copies_of(meta):
    try:
        return max(1, min(999, int(meta.get("copies") or 1)))
    except (TypeError, ValueError):
        return 1


def print_job(store, meta, tray=None):
    cfg = store.cfg
    n = copies_of(meta)
    if not meta.get("printable"):
        raise RuntimeError("this job has no print sheet")
    opts = []
    for o in cfg.print_options.split():
        if tray in TRAYS and o.startswith("media="):
            o = "media=" + TRAYS[tray]
        opts += ["-o", o]
    if tray in TRAYS and not any(o.startswith("media=") for o in opts):
        opts += ["-o", "media=" + TRAYS[tray]]
    cmd = ["lp"] + (["-d", cfg.printer] if cfg.printer else []) + opts + \
        ["-n", str(n), "-t", meta["id"], str(Path(meta["folder"]) / "print.pdf")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "lp failed")
    where = f"tray {tray}" if tray in TRAYS else "default tray"
    store.note(meta["id"], f"sent {n} {'copy' if n == 1 else 'copies'} to printer {cfg.printer or '(default)'}, {where}",
               prints=meta.get("prints", 0) + n, printed_at=now_iso(), sheets_cut=0, cut_at="")
    return r.stdout.strip()


def cut_job(store, meta):
    data = (Path(meta["folder"]) / "cut.plt").read_text()
    msg = cutter.send(data, store.cfg)
    store.note(meta["id"], f"sent to cutter: {msg}", cuts=meta.get("cuts", 0) + 1, cut_at=now_iso(),
               sheets_cut=meta.get("sheets_cut", 0) + 1)
    return msg


class QRMode:
    """Cutter-driven loop, like SignCut's spooler in QR mode: for every sheet
    in the feed tray, read its QR code, cut the matching job (or feed the
    sheet straight through if the code is unknown), and go on to the next
    sheet until the tray is empty or the user stops."""

    def __init__(self, store):
        self.store, self.on, self.status, self.thread = store, False, "off", None
        self.done = self.ejected = 0
        self.paused = False

    def set(self, on):
        if on and not self.on:
            self.on, self.paused = True, False
            self.done = self.ejected = 0
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
        elif not on:
            self.on = False
            self.status = "stopping after this sheet"

    def pause(self, paused):
        self.paused = paused and self.on
        if self.on:
            self.say("paused: finishes this sheet, then waits" if self.paused else "resumed")

    def say(self, msg):
        self.status = msg
        log(f"QR mode: {msg}")

    def _trace(self, direction, text):
        if self.store.cfg.cutter_trace:
            log(f"cutter {direction} {text.strip()}")

    def run(self):
        cfg = self.store.cfg
        empty_since = None
        while self.on:
            try:
                with cutter.Link(cfg) as link:
                    link.trace = self._trace
                    self.say("connected, checking the feed tray")
                    cam = None
                    while self.on:
                        if self.paused:
                            self.status = f"paused ({self.done} cut, {self.ejected} ejected)"
                            time.sleep(1)
                            continue
                        link.send("CMD:320;")
                        loaded = link.expect("CMD:320,", 5)
                        if loaded is None:
                            self.say("cutter does not answer the feed check (CMD:320); retrying")
                            time.sleep(3)
                            continue
                        if loaded.strip() != "4":
                            empty_since = empty_since or time.time()
                            self.status = (f"feed tray empty: {self.done} cut, {self.ejected} ejected. "
                                           "Load sheets to carry on, or switch QR mode off")
                            time.sleep(2)
                            continue
                        empty_since = None
                        # Tested on the SC-350 with qrprobe (2026-10-31): find the 4 marks
                        # first (CMD:318 + CMD:307), then move the camera over the QR code
                        # (its centre plus the camera offset from CMD:122) and read it. The
                        # mark search loads a sheet, so it can't come after the QR read.
                        span = cutter.mark_span(cfg, cutter.cfg_sheet(cfg))
                        sx, sy = cutter._span_units(span, cfg)
                        o = cfg.mark_origin_units
                        eject = f"PU0,0;!PG;PU;CMD:319,{sx + 800},{sy},0;!PG;;"
                        self.say("feeding the sheet in and finding the marks")
                        link.send(f"IN;CMD:318,{sx},{sy},{cfg.feed_params};CMD:307,{o},{o},{sx},{sy};")
                        fed = link.expect("CMD:318,", cfg.qr_timeout)
                        found = link.expect("CMD:307,", cfg.qr_timeout) if fed and fed.strip().upper().startswith("OK") else None
                        if found is None or not found.strip().upper().startswith("OK"):
                            self.ejected += 1
                            self.say(f"the cutter could not feed the sheet or find its marks "
                                     f"(feed {fed or 'no answer'}, marks {found or 'no answer'}), ejecting it")
                            link.send(eject)
                            link.expect("CMD:319,", 120)
                            self._wait_idle(link)
                            continue
                        if cam is None:
                            link.send("CMD:122;")
                            r = link.expect("CMD:122,", 10) or ""
                            try:
                                cam = tuple(int(float(v)) for v in r.split(",")[:2])
                            except ValueError:
                                cam = (0, 0)
                            self.say(f"camera offset {cam}")
                        qx, qy = o + cam[0], o + round(sy / 2) + cam[1]
                        self.say("reading the QR code")
                        link.send(f"PU{qx},{qy};")
                        link.read(3)    # let the head get there (qrprobe waited 3 s too)
                        link.send("CMD:305,1;CMD:304;CMD:305,0;")
                        qr = link.expect("CMD:304,", cfg.qr_timeout, give_up=("NOQRCODE",))
                        hits = self.store.find(qr) if qr else []
                        meta = next((m for m in hits if m.get("status") == "queued"), hits[0] if hits else None)
                        plt = Path(meta["folder"]) / "cut.plt" if meta else None
                        if not meta or not plt.exists():
                            self.ejected += 1
                            self.say(f"no job for QR '{qr}', ejecting the sheet" if qr else "no QR code found, ejecting the sheet")
                            link.send(eject)
                            link.expect("CMD:319,", 120)
                            self._wait_idle(link)
                            continue
                        # the job's own IN / CMD:318 / CMD:307 are done already
                        parts = plt.read_text().strip().lstrip("$").rstrip("#").split(";")
                        head = []
                        while parts and (parts[0].strip() == "IN" or parts[0].strip().startswith(("CMD:318", "CMD:307"))):
                            head.append(parts.pop(0).strip())
                        want = f"CMD:318,{sx},{sy},"
                        if any(h.startswith("CMD:318") and not h.startswith(want) for h in head):
                            self.ejected += 1
                            self.say(f"{meta['id']} was made for another sheet size than {cfg.sheet_width_mm:g} x "
                                     f"{cfg.sheet_height_mm:g} mm, ejecting the sheet")
                            link.send(eject)
                            link.expect("CMD:319,", 120)
                            self._wait_idle(link)
                            continue
                        cuts = ";".join(parts)
                        self.say(f"cutting {meta['id']} ({meta['name']})")
                        link.send(cuts)
                        result = link.expect("CMD:319,", cfg.cut_timeout)
                        self._wait_idle(link)
                        self.done += 1
                        # the job counts as cut once every printed copy has been cut
                        meta = self.store.load(meta["id"]) or meta
                        got, want = meta.get("sheets_cut", 0) + 1, copies_of(meta)
                        self.store.note(meta["id"], f"sheet {got} of {want} cut in QR mode ({'finished' if result else 'no finish reply'})",
                                        cuts=meta.get("cuts", 0) + 1, sheets_cut=got,
                                        **({"cut_at": now_iso()} if got >= want else {}))
                        self.say(f"{meta['id']} sheet {got} of {want} cut; {self.done} cut so far, next sheet")
                    self.say(f"stopped: {self.done} cut, {self.ejected} ejected")
            except Exception as e:
                self.say(f"problem: {e}; retrying in 10 s")
                for _ in range(10):
                    if not self.on:
                        break
                    time.sleep(1)
        self.status = f"off ({self.done} cut, {self.ejected} ejected)" if self.done or self.ejected else "off"

    def _wait_idle(self, link, limit=120):
        """Poll CMD:100 (as SignCut's spooler does) until the cutter says OK,
        so the next sheet is only asked for once this one is out."""
        end = time.time() + limit
        while time.time() < end:
            link.send("CMD:100;")
            r = link.expect("CMD:100,", 5)
            if r is not None and r.strip().upper().startswith("OK"):
                return True
            time.sleep(1)
        return False


def make_handler(store: Store, qr: "QRMode"):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
            if not parts:
                return self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
            if parts == ["api", "jobs"]:
                jobs = store.all()
                for j in jobs:
                    j.pop("folder", None)
                    j["qr"] = store.qr_text(j["id"])
                media = next((o[6:] for o in store.cfg.print_options.split() if o.startswith("media=")), "")
                default_tray = next((k for k, v in TRAYS.items() if v == media), "")
                return self._send(200, {"jobs": jobs, "queue_hours": store.cfg.queue_hours, "tray": default_tray,
                                        "hotfolder": str(store.cfg.hotfolder),
                                        "qr_mode": {"on": qr.on, "paused": qr.paused, "status": qr.status, "done": qr.done}})
            if parts == ["api", "find"]:
                q = parse_qs(u.query).get("q", [""])[0]
                return self._send(200, {"ids": [m["id"] for m in store.find(q)]})
            if len(parts) == 3 and parts[0] == "files" and parts[2] in FILES:
                meta = store.load(parts[1])
                f = meta and Path(meta["folder"]) / parts[2]
                if not f or not f.exists():
                    return self._send(404, {"error": "not found"})
                ctype = mimetypes.guess_type(f.name)[0] or "text/plain"
                return self._send(200, f.read_bytes(), ctype)
            self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
            length = int(self.headers.get("Content-Length") or 0)
            try:
                if parts == ["api", "upload"]:
                    name = Path(unquote(self.headers.get("X-Filename", "upload"))).name
                    if not name or name.startswith("."):
                        return self._send(400, {"error": "bad file name"})
                    tmp = store.cfg.hotfolder / f".{name}.part"
                    with open(tmp, "wb") as f:
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(remaining, 1 << 20))
                            if not chunk:
                                break
                            f.write(chunk)
                            remaining -= len(chunk)
                    tmp.replace(store.cfg.hotfolder / name)
                    return self._send(200, {"ok": True})
                body = json.loads(self.rfile.read(length) or b"{}")
                if parts == ["api", "qrmode"]:
                    if "pause" in body:
                        qr.pause(bool(body["pause"]))
                        return self._send(200, {"ok": True, "message": "paused" if qr.paused else "resumed"})
                    qr.set(bool(body.get("on")))
                    return self._send(200, {"ok": True, "message": "QR mode " + ("on" if qr.on else "off")})
                if parts == ["api", "printmany"]:
                    done, skipped, failed = [], [], []
                    for jid in body.get("ids", []):
                        m = store.load(jid)
                        if not m or m.get("error") or not m.get("printable"):
                            failed.append(jid); continue
                        if m.get("printed_at") and not body.get("again"):
                            skipped.append(jid); continue
                        try:
                            print_job(store, m, str(body.get("tray") or "") or None)
                            done.append(jid)
                        except Exception as e:
                            failed.append(f"{jid} ({e})")
                    msg = f"sent {len(done)} to the printer"
                    if skipped:
                        msg += f", skipped {len(skipped)} already printed"
                    if failed:
                        msg += f", could not print {', '.join(failed)}"
                    return self._send(200, {"ok": True, "message": msg, "skipped": skipped})
                if len(parts) != 3 or parts[0] != "api":
                    return self._send(404, {"error": "not found"})
                jid, action = parts[1], parts[2]
                meta = store.load(jid)
                if not meta:
                    return self._send(404, {"error": f"no job {jid}"})
                again = {"print": "printed_at", "cut": "cut_at"}.get(action)
                if again and meta.get(again) and not body.get("again"):
                    # already done for this order: ask before doing it twice
                    return self._send(409, {"confirm": f"{jid} was already {'printed' if action == 'print' else 'cut'} "
                                                       f"at {meta[again][11:16]} on {meta[again][:10]}. Do it again?"})
                if action == "print":
                    msg = print_job(store, meta, str(body.get("tray") or "") or None)
                elif action == "cut":
                    msg = cut_job(store, meta)
                elif action == "mark":
                    field = {"printed": "printed_at", "cut": "cut_at"}[body.get("what")]
                    on = bool(body.get("on"))
                    extra = {"sheets_cut": copies_of(meta) if on else 0} if field == "cut_at" else {}
                    store.note(jid, f"marked {'as' if on else 'not'} {body['what']} by hand", **{field: now_iso() if on else ""}, **extra)
                    msg = f"marked {'' if on else 'not '}{body['what']}"
                elif action == "requeue":
                    store.requeue(jid); msg = "back in the cut queue"
                elif action == "archive":
                    store.archive(jid, why="archived by hand"); msg = "archived"
                elif action == "rebuild":
                    store.prepare(jid); msg = "rebuilt"
                elif action == "delete":
                    store.delete(jid); msg = "deleted"
                elif action == "copies":
                    n = copies_of({"copies": body.get("copies")})
                    store.note(jid, f"copies set to {n}", copies=n)
                    msg = f"{n} {'copy' if n == 1 else 'copies'}"
                elif action == "customer":
                    store.note(jid, "customer set", customer=str(body.get("customer", ""))[:200])
                    msg = "saved"
                else:
                    return self._send(404, {"error": "unknown action"})
                return self._send(200, {"ok": True, "message": msg})
            except Exception as e:  # report to the page instead of dropping the request
                log(traceback.format_exc())
                return self._send(500, {"error": str(e)})

    return H


def run(cfg):
    store = Store(cfg, log=log)
    threading.Thread(target=worker, args=(store,), daemon=True).start()
    srv = ThreadingHTTPServer((cfg.listen, cfg.port), make_handler(store, QRMode(store)))
    log(f"SC-350 cut queue on http://{cfg.listen}:{cfg.port}  hot folder {cfg.hotfolder}")
    srv.serve_forever()
