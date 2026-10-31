"""Settings, read from /etc/sc350cut.ini (or the file given with --config)."""
import configparser
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = "/etc/sc350cut.ini"


@dataclass
class Config:
    # folders
    hotfolder: Path = Path("/srv/cut/hotfolder")
    queue: Path = Path("/srv/cut/queue")
    archive: Path = Path("/srv/cut/archive")
    queue_hours: float = 24.0

    # web page
    listen: str = "0.0.0.0"
    port: int = 8350

    # jobs
    job_prefix: str = "ALT"
    qr_text: str = "{job_id}"          # what the QR code holds
    reuse_by_filename: bool = True     # same file name again = same job, new revision

    # which lines in the artwork are cut lines
    cut_names: list = field(default_factory=lambda: ["cutcontour", "cut contour", "cut", "thru-cut", "kiss-cut"])
    cut_colors: list = field(default_factory=lambda: ["#ff00ff", "#ec008c", "#e6007e"])

    # print sheet with registration marks
    sheet_width_mm: float = 297.0
    sheet_height_mm: float = 420.0
    mark_size_mm: float = 5.0          # mark diameter (dot) or edge (square)
    mark_inset_mm: float = 10.0        # sheet edge to the mark's outer edge
    mark_shape: str = "dot"            # dot (SignCut Vulcan) or square
    mark_count: int = 4                # 3 or 4 corner marks
    qr_size_mm: float = 12.0
    printer: str = ""                  # CUPS queue name of the Xerox, e.g. Xerox_C8000
    # lp -o options. The Xerox C60 aborts jobs that name no tray (error 024-747);
    # media=manual is Tray 5 (bypass), media=tray-1 is Tray 1; both printed.
    print_options: str = "media=tray-1 print-color-mode=color"

    # cutter
    cutter_device: str = "tcp://192.168.0.52:9100"   # or /dev/usb/lp0, /dev/ttyUSB0, cups:QueueName
    cutter_baud: int = 38400
    protocol: str = "signcut"          # signcut = CMD:318/319 feed framing; plain = preamble/postamble only
    feed_params: str = "600,600,0"     # last three CMD:318 values, as in SignCut's QR job files
    qr_timeout: float = 60.0           # QR mode: seconds to wait for the cutter to read a QR code
    units_per_mm: float = 40.0
    speed: int = 0                     # VS value, 0 = leave cutter default
    force: int = 0                     # FS value, 0 = leave cutter default
    origin: str = "mark1"              # cut 0,0: mark1 = centre of the bottom-left mark (as SignCut), mark1corner, or sheet
    mark_origin_units: int = 100       # plotter units at the first mark's centre (SignCut: 100,100)
    rotate_portrait: bool = True       # turn portrait sheets so the long side runs along the feed
    preamble: str = "IN;"
    postamble: str = "PU0,0;SP0;"
    mirror: bool = False
    plt_export: Path = Path("/srv/cut/plt")   # every job's PLT, named by its QR text


def _list(v):
    return [x.strip() for x in v.split(",") if x.strip()]


def load(path=DEFAULT_PATH) -> Config:
    cfg = Config()
    p = Path(path)
    if not p.exists():
        return cfg
    cp = configparser.ConfigParser()
    cp.read(p)
    for section in cp.sections():
        for key, raw in cp.items(section):
            if not hasattr(cfg, key):
                continue
            cur = getattr(cfg, key)
            if isinstance(cur, bool):
                val = cp.getboolean(section, key)
            elif isinstance(cur, int):
                val = int(raw)
            elif isinstance(cur, float):
                val = float(raw)
            elif isinstance(cur, Path):
                val = Path(raw)
            elif isinstance(cur, list):
                val = _list(raw)
            else:
                val = raw
            setattr(cfg, key, val)
    return cfg
