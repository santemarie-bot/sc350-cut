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
from .jobs import Store

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


def print_job(store, meta):
    cfg = store.cfg
    if not meta.get("printable"):
        raise RuntimeError("this job has no print sheet")
    opts = []
    for o in cfg.print_options.split():
        opts += ["-o", o]
    cmd = ["lp"] + (["-d", cfg.printer] if cfg.printer else []) + opts + \
        ["-t", meta["id"], str(Path(meta["folder"]) / "print.pdf")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "lp failed")
    store.note(meta["id"], f"sent to printer {cfg.printer or '(default)'}", prints=meta.get("prints", 0) + 1)
    return r.stdout.strip()


def cut_job(store, meta):
    data = (Path(meta["folder"]) / "cut.plt").read_text()
    msg = cutter.send(data, store.cfg)
    store.note(meta["id"], f"sent to cutter: {msg}", cuts=meta.get("cuts", 0) + 1)
    return msg


class QRMode:
    """Cutter-driven loop, like SignCut's spooler in QR mode: wait for a
    sheet, feed it in, let the camera read the QR code, send that job's cut
    lines, feed the sheet out, repeat until stopped."""

    def __init__(self, store):
        self.store, self.on, self.status, self.thread = store, False, "off", None
        self.done = 0

    def set(self, on):
        if on and not self.on:
            self.on = True
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
        elif not on:
            self.on = False
            self.status = "stopping"

    def say(self, msg):
        self.status = msg
        log(f"QR mode: {msg}")

    def run(self):
        cfg = self.store.cfg
        while self.on:
            try:
                with cutter.Link(cfg) as link:
                    self.say("connected, waiting for a sheet")
                    while self.on:
                        link.send("CMD:320;")
                        loaded = link.expect("CMD:320,", 5)
                        if loaded is None:
                            self.say("cutter does not answer CMD:320 (material check) on this port")
                            time.sleep(5)
                            continue
                        if loaded.strip() != "4":
                            self.status = "waiting for a sheet"
                            time.sleep(2)
                            continue
                        self.say("reading the QR code")
                        link.send("CMD:305,1;CMD:304;CMD:305,0;")
                        qr = link.expect("CMD:304,", cfg.qr_timeout, give_up=("NOQRCODE",))
                        hits = self.store.find(qr) if qr else []
                        meta = next((m for m in hits if m.get("status") == "queued"), hits[0] if hits else None)
                        if not meta or not (Path(meta["folder"]) / "cut.plt").exists():
                            self.say(f"no job for QR '{qr}', sheet fed through" if qr else "no QR code read, sheet fed through")
                            link.send(cutter.EMPTY_JOB)
                            time.sleep(3)
                            continue
                        job = (Path(meta["folder"]) / "cut.plt").read_text().strip().lstrip("$").rstrip("#")
                        self.say(f"cutting {meta['id']}")
                        link.send(job)
                        result = link.expect("CMD:307,", 600)
                        self.done += 1
                        self.store.note(meta["id"], f"cut in QR mode ({result or 'no result reply'})",
                                        cuts=meta.get("cuts", 0) + 1)
                        self.say(f"{meta['id']} cut: {result or 'no result reply'}; waiting for the next sheet")
                        time.sleep(1)
            except Exception as e:
                self.say(f"problem: {e}; retrying in 10 s")
                for _ in range(10):
                    if not self.on:
                        break
                    time.sleep(1)
        self.status = "off"


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
                return self._send(200, {"jobs": jobs, "queue_hours": store.cfg.queue_hours,
                                        "hotfolder": str(store.cfg.hotfolder),
                                        "qr_mode": {"on": qr.on, "status": qr.status, "done": qr.done}})
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
                    qr.set(bool(body.get("on")))
                    return self._send(200, {"ok": True, "message": "QR mode " + ("on" if qr.on else "off")})
                if len(parts) != 3 or parts[0] != "api":
                    return self._send(404, {"error": "not found"})
                jid, action = parts[1], parts[2]
                meta = store.load(jid)
                if not meta:
                    return self._send(404, {"error": f"no job {jid}"})
                if action == "print":
                    msg = print_job(store, meta)
                elif action == "cut":
                    msg = cut_job(store, meta)
                elif action == "requeue":
                    store.requeue(jid); msg = "back in the cut queue"
                elif action == "archive":
                    store.archive(jid, why="archived by hand"); msg = "archived"
                elif action == "rebuild":
                    store.prepare(jid); msg = "rebuilt"
                elif action == "delete":
                    store.delete(jid); msg = "deleted"
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
