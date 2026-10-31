# SC-350 cut software for Ubuntu

Replaces SignCut Pro + spooler in the Windows VM for the Vulcan SC-350 sheet cutter.

## What it does

1. **Hot folder** (`/srv/cut/hotfolder`): drop artwork (PDF, AI, SVG, EPS, or a ready PLT). It's picked up within a few seconds.
2. **Job + QR**: each new artwork gets a job ID such as `ALT-261028-001`. The software lays it on the print sheet with 4 registration marks (5 mm, 10 mm from the edge, same as SignCut's Vulcan defaults) and a QR code holding the job ID, and saves `print.pdf`. The cut lines are taken out of `print.pdf`, so the Xerox prints only the artwork, marks and QR code.
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

## Cutter commands

Every cut file is wrapped the way SignCut writes it for the Vulcan (see `notes/signcut-protocol.md` in the project files):
`IN;CMD:318,<length>,<width>,200,200,1;` feeds the sheet in and finds the marks, then the cut lines (0.025 mm units,
measured from the outer corner of the bottom-left mark), then `CMD:319,...` feeds the sheet out.
Portrait sheets are turned so the long side runs along the feed.

**QR mode** (button at the top of the page): the server keeps a connection to the cutter, waits for a sheet
(`CMD:320`), feeds it in, asks the camera for the QR code (`CMD:304`), sends the matching job's cut lines,
and feeds the sheet out. Load sheets one after another; each is cut by its own QR code.

## Still to confirm on the real machine

- Which TCP port the cutter answers on (9100 or 9200). Test with
  `python3 -m sc350cut --config /etc/sc350cut.ini talk 'CMD:100;'` and add `--port 9200` to try the other one.
- Whether the camera finds our square marks and QR code where we print them, or needs SignCut's layout.

## Command line

```
python3 -m sc350cut --config /etc/sc350cut.ini ingest file.pdf   # add a file now
python3 -m sc350cut --config /etc/sc350cut.ini send job.plt      # send a PLT to the cutter
python3 -m sc350cut --config /etc/sc350cut.ini talk 'CMD:100;'   # send a command, show the cutter's reply
```
