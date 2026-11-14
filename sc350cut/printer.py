"""Is the Xerox reachable? Checked before a job is queued in CUPS, so nothing piles up while it is off."""
import re
import socket
import subprocess
import time
from urllib.parse import urlparse

_cache = {"at": 0.0, "key": None, "val": None}


def _device_uri(queue):
    """Device URI of the CUPS queue (empty queue name = the default printer)."""
    try:
        if not queue:
            out = subprocess.run(["lpstat", "-d"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"destination:\s*(\S+)", out)
            queue = m.group(1) if m else ""
        out = subprocess.run(["lpstat", "-v", queue] if queue else ["lpstat", "-v"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"device for \S+:\s*(\S+)", out)
        return m.group(1) if m else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def check(queue, timeout=2.0, max_age=5.0):
    """(online, host, message). Opens a TCP connection to the printer's host:port (631 for ipp, 9100 for socket)."""
    now = time.time()
    if _cache["key"] == queue and now - _cache["at"] < max_age:
        return _cache["val"]
    uri = _device_uri(queue)
    u = urlparse(uri)
    if not u.hostname:
        val = (True, "", "no network address for this queue; not checked")   # e.g. a local USB queue
    else:
        port = u.port or {"ipp": 631, "ipps": 631, "http": 80, "https": 443, "socket": 9100, "lpd": 515}.get(u.scheme, 631)
        try:
            socket.create_connection((u.hostname, port), timeout=timeout).close()
            val = (True, u.hostname, "")
        except OSError as e:
            val = (False, u.hostname, f"Xerox offline: {u.hostname} is not answering ({e.strerror or e})")
    _cache.update(at=now, key=queue, val=val)
    return val
