"""Ask CUPS what happened to a print job (IPP Get-Job-Attributes on localhost:631).

lp returns as soon as CUPS has queued the file, so a job is only marked printed
once CUPS says it completed; a job CUPS stopped, aborted or cancelled is shown
as failed with CUPS's reason."""
import http.client
import re
import struct

STATES = {3: "pending", 4: "held", 5: "printing", 6: "stopped", 7: "cancelled", 8: "aborted", 9: "completed"}
DONE_OK, DONE_BAD = {9}, {7, 8}


def job_id_from_lp(stdout: str):
    """'request id is XeroxC60-123 (1 file(s))' -> 123"""
    m = re.search(r"request id is \S+-(\d+)", stdout or "")
    return int(m.group(1)) if m else None


def _attr(tag, name, value: bytes, first=True):
    n = name.encode() if first else b""
    return struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(value)) + value


def _request(job_id):
    b = struct.pack(">BBHI", 2, 0, 0x0009, 1) + b"\x01"
    b += _attr(0x47, "attributes-charset", b"utf-8")
    b += _attr(0x48, "attributes-natural-language", b"en")
    b += _attr(0x45, "job-uri", f"ipp://localhost/jobs/{job_id}".encode())
    b += _attr(0x42, "requesting-user-name", b"sc350cut")
    b += _attr(0x44, "requested-attributes", b"job-state")
    b += _attr(0x44, "", b"job-state-reasons", first=False)
    b += _attr(0x44, "", b"job-printer-state-message", first=False)
    return b + b"\x03"


def _parse(data: bytes):
    status = struct.unpack(">H", data[2:4])[0]
    i, attrs, name = 8, {}, None
    while i < len(data):
        tag = data[i]; i += 1
        if tag < 0x10:  # group delimiter
            if tag == 0x03:
                break
            continue
        nl = struct.unpack(">H", data[i:i + 2])[0]; i += 2
        if nl:
            name = data[i:i + nl].decode(errors="replace")
        i += nl
        vl = struct.unpack(">H", data[i:i + 2])[0]; i += 2
        v = data[i:i + vl]; i += vl
        val = struct.unpack(">i", v)[0] if tag in (0x21, 0x23) and vl == 4 else v.decode(errors="replace")
        attrs.setdefault(name, []).append(val)
    return status, attrs


def job_state(job_id, host="localhost", port=631, timeout=5):
    """(state number, 'state: reasons / message') or (None, error text)."""
    try:
        c = http.client.HTTPConnection(host, port, timeout=timeout)
        c.request("POST", "/jobs", _request(job_id), {"Content-Type": "application/ipp"})
        r = c.getresponse()
        data = r.read()
        c.close()
        status, a = _parse(data)
        if status >= 0x0400 or "job-state" not in a:
            return None, f"CUPS answered 0x{status:04x} for job {job_id}"
        st = a["job-state"][0]
        why = [x for x in a.get("job-state-reasons", []) if x and x not in ("none", "processing-to-stop-point")]
        why += [x for x in a.get("job-printer-state-message", []) if x]
        return st, STATES.get(st, str(st)) + (": " + ", ".join(why) if why else "")
    except OSError as e:
        return None, f"could not ask CUPS: {e}"
