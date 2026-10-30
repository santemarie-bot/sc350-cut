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
    mark_size_mm: float = 5.0          # square mark edge
    mark_inset_mm: float = 10.0        # mark centre distance from sheet edge
    mark_count: int = 4                # 3 or 4 corner marks
    qr_size_mm: float = 12.0
    printer: str = ""                  # CUPS queue name of the Xerox, e.g. Xerox_C8000

    # cutter
    cutter_device: str = "tcp://192.168.0.52:9100"   # or /dev/usb/lp0, /dev/ttyUSB0, cups:QueueName
    cutter_baud: int = 9600
    units_per_mm: float = 40.0
    speed: int = 0                     # VS value, 0 = leave cutter default
    force: int = 0                     # FS value, 0 = leave cutter default
    origin: str = "mark1"              # cut origin: mark1 (bottom-left mark) or sheet
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
