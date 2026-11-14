"""Held jobs, counter confirmation and retries, against a FAKE Xerox (no real printer, no real CUPS)."""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sc350cut import cupsjob, held, printer, server
from sc350cut.config import Config
from sc350cut.jobs import Store


class FakeXerox:
    """Controls everything the code asks about the Xerox: reachable?, counter, CUPS job states, lp calls."""
    def __init__(self):
        self.online, self.idle, self.total, self.next_job = True, True, 1000, 100
        self.lp_calls, self.cups = [], {}          # cups: job -> state number
        self.prints_ok = True                      # False: CUPS says completed but the page is not counted

    def install(self):
        printer.check = lambda q, **k: (self.online, "10.0.0.51", "" if self.online else "Xerox offline: fake")
        printer.queue_idle = lambda q: self.online and self.idle
        printer.counters = lambda q: {"total": self.total, "color": self.total, "large": 0} if self.online else None
        cupsjob.job_state = lambda j: (self.cups[j], {9: "completed", 5: "printing"}[self.cups[j]]) if j in self.cups else (None, "?")

        def run(cmd, **kw):
            assert cmd[0] == "lp", cmd
            self.lp_calls.append(cmd)
            self.next_job += 1
            self.cups[self.next_job] = 9               # CUPS completes it at once
            if self.prints_ok:
                self.total += 2                         # A3 colour = 2 impressions
            class R: returncode = 0; stderr = ""
            R.stdout = f"request id is XeroxC60-{self.next_job} (1 file(s))\n"
            return R
        subprocess.run = run


class T(unittest.TestCase):
    def setUp(self):
        self.real_run = subprocess.run
        self.d = Path(tempfile.mkdtemp())
        self.cfg = Config(hotfolder=self.d / "h", queue=self.d / "q", archive=self.d / "a", held_dir=self.d / "held",
                          escalation_log=self.d / "esc.log", held_watch_s=0.0, held_warmup_s=60.0, held_max_retries=3)
        self.cfg.plt_export = self.d / "p"
        self.store = Store(self.cfg, log=lambda *a: None)
        self.x = FakeXerox(); self.x.install()
        held._online_since[0] = None; held._last[0] = 0.0; server._last_watch[0] = 0.0
        self.t = 1_000_000.0
        for n in ("ALT-1", "ALT-2"):
            jf = self.d / "q" / n; jf.mkdir()
            (jf / "print.pdf").write_bytes(b"%PDF")
            self.store.jobs[n] = jf
            self.store.save({"id": n, "folder": str(jf), "printable": True, "name": n + ".pdf", "history": []})

    def tearDown(self):
        subprocess.run = self.real_run

    def m(self, n="ALT-1"):
        return self.store.load(n)

    def watch(self, secs=0):
        self.t += secs
        return held.release_next(self.store, lambda meta, e: server.release_held(self.store, meta, e), now=self.t)

    def release(self):
        """Run the watcher (advancing time past the warm-up) until it releases a job."""
        for _ in range(3):
            r = self.watch(61)
            if r.startswith("released"):
                return r
        return r

    def poll(self):
        server._last_watch[0] = 0.0; server.watch_prints(self.store)

    def test_offline_is_held_not_sent(self):
        self.x.online = False
        msg = server.print_job(self.store, self.m(), "5", 1)
        self.assertIn("Waiting for Xerox", msg)
        self.assertEqual(self.x.lp_calls, [])
        self.assertEqual((self.m()["print_state"], self.m()["print_msg"], self.m()["printed_at"]), ("held", "Waiting for Xerox", ""))
        self.assertEqual(len(held.entries(self.cfg)), 1)
        self.assertIn("already waiting", server.print_job(self.store, self.m(), "5", 1))     # no duplicate hold
        self.assertEqual(len(held.entries(self.cfg)), 1)

    def test_release_waits_for_warmup_idle_and_goes_one_at_a_time_oldest_first(self):
        self.x.online = False
        server.print_job(self.store, self.m("ALT-1"), "5", 1); time.sleep(1.1)
        server.print_job(self.store, self.m("ALT-2"), "5", 1)
        self.assertEqual(self.watch(), "offline")
        self.x.online = True
        self.assertEqual(self.watch(), "warming up")                     # just came back
        self.assertEqual(self.watch(30), "warming up")
        self.x.idle = False
        self.assertEqual(self.watch(40), "queue busy")
        self.x.idle = True
        self.assertEqual(self.watch(1), "released ALT-1")                # oldest first
        self.assertEqual(len(self.x.lp_calls), 1)
        self.x.cups[self.x.next_job] = 5                                  # ALT-1 still printing in CUPS
        self.assertEqual(self.watch(1), "previous job still printing")   # ALT-2 waits
        self.x.cups[self.x.next_job] = 9; self.poll()
        self.assertEqual(self.m("ALT-1")["print_state"], "done")
        self.assertEqual(self.watch(1), "released ALT-2")
        self.assertEqual(len(self.x.lp_calls), 2)
        self.poll(); self.assertEqual(self.m("ALT-2")["print_state"], "done")
        self.assertEqual(self.watch(1), "")                               # nothing left, nothing sent twice
        self.assertEqual(len(self.x.lp_calls), 2)

    def test_counter_confirms(self):
        server.print_job(self.store, self.m(), "5", 1); self.poll()
        m = self.m(); self.assertEqual((m["print_state"], m["print_confirmed_by"]), ("done", "xerox-counter"))
        self.assertTrue(m["printed_at"])

    def test_completed_without_counter_goes_back_to_held_and_retries_then_escalates(self):
        self.x.prints_ok = False
        server.print_job(self.store, self.m(), "5", 1)
        for attempt in range(1, 5):                                       # 1 print + 3 retries
            self.poll()                                                   # first sight: waits for the counter
            self.assertEqual(self.m()["print_state"], "sent")
            self.store.note("ALT-1", "age", cups_done_at=time.time() - 61)
            self.poll()
            if attempt <= 3:
                self.assertEqual(self.m()["print_state"], "held", attempt)
                self.assertEqual(len(held.entries(self.cfg)), 1)           # exactly one hold: no duplicates
                self.assertEqual(self.release(), "released ALT-1")
                self.assertEqual(len(self.x.lp_calls), attempt + 1)
        m = self.m()
        self.assertEqual(m["print_state"], "escalated"); self.assertFalse(m["printed_at"])
        self.assertEqual(len(self.x.lp_calls), 4)                         # never more than 1 + 3
        self.assertEqual(len(held.entries(self.cfg)), 0)
        self.assertIn("ALT-1", (self.d / "esc.log").read_text())
        self.assertEqual(server.printed_sheets(m), 0)                     # failed attempts are not counted as printed

    def test_retry_then_success_counts_one_print(self):
        self.x.prints_ok = False
        server.print_job(self.store, self.m(), "5", 1); self.poll()
        self.store.note("ALT-1", "age", cups_done_at=time.time() - 61); self.poll()
        self.assertEqual(self.m()["print_state"], "held")
        self.x.prints_ok = True
        self.assertEqual(self.release(), "released ALT-1"); self.poll()
        m = self.m(); self.assertEqual(m["print_state"], "done"); self.assertEqual(server.printed_sheets(m), 1)

    def test_unreadable_counter_is_never_retried(self):
        """No positive evidence the page was missed -> trust CUPS, never print again on a guess."""
        real = printer.counters
        printer.counters = lambda q: None
        server.print_job(self.store, self.m(), "5", 1); self.poll()
        self.assertEqual(self.m()["print_state"], "done"); self.assertEqual(len(self.x.lp_calls), 1)
        printer.counters = real

    def test_claim_is_atomic(self):
        self.x.online = False
        server.print_job(self.store, self.m(), "5", 1)
        p = held.entries(self.cfg)[0][0]
        self.assertIsNotNone(held.claim(p)); self.assertIsNone(held.claim(p))
        self.assertTrue(held.is_held(self.cfg, "ALT-1"))                  # a claimed hold still blocks a duplicate


if __name__ == "__main__":
    unittest.main()
