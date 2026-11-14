"""Held print jobs: never lose a print when the Xerox is off.

A job that cannot go to CUPS (Xerox offline) or whose CUPS job ended 'completed' without the Xerox counting a page
is written to cfg.held_dir as <timestamp>-<job id>.json and shown as "Waiting for Xerox". A watcher checks the Xerox
every cfg.held_watch_s; once it has answered for cfg.held_warmup_s and CUPS is idle, held jobs are released ONE AT A
TIME, oldest first, and the next one waits until the previous has finished. One job = one hold = one print: a second
hold for the same job is refused, and a hold is claimed by an atomic rename before anything is sent."""
import json
import os
import time
from pathlib import Path

from . import printer

_online_since = [None]          # when the Xerox started answering (for the warm-up)
_last = [0.0]


def _dir(cfg):
    d = Path(cfg.held_dir); d.mkdir(parents=True, exist_ok=True); return d


def entries(cfg):
    """Held entries, oldest first: [(path, dict)]."""
    out = []
    for p in sorted(_dir(cfg).glob("*.json")):
        try:
            out.append((p, json.loads(p.read_text())))
        except (OSError, ValueError):
            continue
    return out


def is_held(cfg, jid):
    return any(e.get("job_id") == jid for _, e in entries(cfg)) or any(_dir(cfg).glob(f"*-{jid}.json.sending"))


def hold(cfg, jid, tray, copies, reason, attempts=0, now=None):
    """Write a hold. Returns its path, or None if this job is already held (no duplicates)."""
    if is_held(cfg, jid):
        return None
    t = now or time.time()
    p = _dir(cfg) / f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime(t))}-{jid}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"job_id": jid, "tray": tray or "", "copies": copies, "reason": reason,
                               "attempts": attempts, "held_at": t}))
    os.replace(tmp, p)
    return p


def claim(path):
    """Atomically take a hold for sending (a second watcher or a restart cannot send it twice)."""
    new = path.with_suffix(".json.sending")
    try:
        os.rename(path, new)
        return new
    except OSError:
        return None


def finish(path, ok, note=""):
    """After sending: sent/ on success; back to held (same name) on failure so it is never lost."""
    if ok:
        d = path.parent / "sent"; d.mkdir(exist_ok=True)
        os.replace(path, d / path.name.replace(".sending", ""))
    else:
        os.replace(path, path.with_name(path.name.replace(".json.sending", ".json")))


def release_next(store, send, log=print, now=None):
    """Called every second by the worker; acts every held_watch_s. send(meta, entry) -> True if CUPS took the job.
    Returns a short string describing what it did (for tests/logs) or ''."""
    cfg = store.cfg
    t = now or time.time()
    if t - _last[0] < cfg.held_watch_s:
        return ""
    _last[0] = t
    items = entries(cfg)
    if not items:
        _online_since[0] = None
        return ""
    online, _h, why = printer.check(cfg.printer, max_age=0)
    if not online:
        _online_since[0] = None
        return "offline"
    if _online_since[0] is None:
        _online_since[0] = t
    if t - _online_since[0] < cfg.held_warmup_s:
        return "warming up"
    if not printer.queue_idle(cfg.printer):
        return "queue busy"
    if any(m and m.get("print_state") == "sent" for m in store.all()):      # one at a time
        return "previous job still printing"
    path, e = items[0]
    meta = store.load(e["job_id"])
    if not meta:
        finish(claim(path) or path, True)                                   # job gone: drop the hold
        return "job gone"
    taken = claim(path)
    if not taken:
        return "claim lost"
    try:
        ok = bool(send(meta, e))
    except Exception as ex:                                                  # keep the hold; try again next time
        log(f"{e['job_id']}: release failed: {ex}")
        ok = False
    finish(taken, ok)
    return ("released " if ok else "release failed ") + e["job_id"]
