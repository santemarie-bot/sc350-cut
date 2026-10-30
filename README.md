# SC-350 cut software for Ubuntu

Replaces SignCut Pro + spooler in the Windows VM for the Vulcan SC-350 sheet cutter.

## What it does

1. **Hot folder** (`/srv/cut/hotfolder`): drop artwork (PDF, AI, SVG, EPS, or a ready PLT). It's picked up within a few seconds.
2. **Job + QR**: each new artwork gets a job ID such as `ALT-261028-001`. The software lays it on the print sheet with 4 registration marks (5 mm, 10 mm from the edge, same as SignCut's Vulcan defaults) and a QR code holding the job ID, and saves `print.pdf`.
3. **Cut file**: the CutContour line (by layer/name, or magenta stroke) becomes `cut.plt` (HP-GL), measured from registration mark 1. A copy named by the QR text goes to `/srv/cut/plt`.
4. **Active cut queue** (web page on port 8350): shows each job with a preview, Print (sends print.pdf to the Xerox through CUPS), Cut (sends to the cutter at 192.168.0.52), and the time left.
5. **Archive**: after 24 hours a job moves to `/srv/cut/archive/YYYY/MM/<job id>`. Search or scan the QR to find it, then "Reuse" puts it back in the queue.
6. **Repeat orders**: dropping the same artwork again puts the existing job straight back in the queue (same job ID, same QR, files ready). Dropping a file with the same name but changed artwork makes a new revision of that job and keeps the old one in `versions/`.

Files that can't be used go to `hotfolder/_rejected` with a `.txt` saying why.

## Install

```
sudo ./deploy/install.sh
```

Settings: `/etc/sc350cut.ini` (copied from `deploy/sc350cut.ini`). Set `printer =` to the Xerox CUPS queue name (`lpstat -p`). Restart with `sudo systemctl restart sc350cut`. Logs: `journalctl -u sc350cut -f`.

## Still to confirm on the real machine

- The SC-350's network port and the exact commands SignCut sends for camera marks and QR mode. Run `sudo ./deploy/capture-signcut.sh`, cut one job from SignCut, and the capture shows them. They then go into `preamble`/`postamble` (or code) so cuts register on the marks.
- Units per mm (40 is the HP-GL standard) and whether the cutter wants mark 1 as origin.

## Command line

```
python3 -m sc350cut --config /etc/sc350cut.ini ingest file.pdf   # add a file now
python3 -m sc350cut --config /etc/sc350cut.ini send job.plt      # send a PLT to the cutter
```
