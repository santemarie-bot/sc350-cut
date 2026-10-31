"""python3 -m sc350cut [--config /etc/sc350cut.ini] [serve|process FILE|send FILE.plt]"""
import argparse
import sys
from pathlib import Path

from . import config, cutter, server
from .jobs import Store


def main():
    ap = argparse.ArgumentParser(prog="sc350cut")
    ap.add_argument("--config", default=config.DEFAULT_PATH)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the hot folder watcher and web page (default)")
    p = sub.add_parser("ingest", help="add one artwork file now, as if dropped in the hot folder")
    p.add_argument("file")
    p = sub.add_parser("send", help="send a PLT file straight to the cutter")
    p.add_argument("file")
    p = sub.add_parser("talk", help="send cutter commands and show the reply, e.g. talk 'CMD:100;'")
    p.add_argument("text")
    p.add_argument("--port", type=int, help="try another TCP port, e.g. 9200")
    sub.add_parser("archive-due", help="archive jobs older than the queue time")
    a = ap.parse_args()
    cfg = config.load(a.config)
    if a.cmd in (None, "serve"):
        server.run(cfg)
    elif a.cmd == "ingest":
        print(Store(cfg).ingest(Path(a.file)))
    elif a.cmd == "send":
        print(cutter.send(Path(a.file).read_text(errors="replace"), cfg))
    elif a.cmd == "talk":
        if a.port:
            host = cfg.cutter_device[6:].partition(":")[0]
            cfg.cutter_device = f"tcp://{host}:{a.port}"
        reply = cutter.talk(a.text, cfg, wait=3)
        print(reply if reply else "(no reply within 3 s)")
    elif a.cmd == "archive-due":
        Store(cfg).archive_due()
    return 0


if __name__ == "__main__":
    sys.exit(main())
