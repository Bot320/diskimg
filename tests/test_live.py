# SPDX-License-Identifier: GPL-3.0-only
"""Live progress of the backup screen (no root needed): rate over the last seconds, compression
ratio and written bytes of the chunked engine, values that keep updating while a write blocks.
Run:  python3 -I tests/test_live.py [WORKDIR]   (WORKDIR must not be on tmpfs)."""
import os
import random
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diskimg import engine as E  # noqa: E402

ok_all = True


def check(c, what) -> None:
    global ok_all
    print(("ok    " if c else "FAIL  ") + what, flush=True)
    ok_all &= bool(c)


class Sink(E.JobSink):
    def __init__(self):
        self.pairs_log: list[tuple[float, dict]] = []
        self.bars: list[tuple[str, float]] = []

    def pairs(self, pairs):
        self.pairs_log.append((time.monotonic(), {k: v for k, v, _a in pairs}))

    def bar(self, which, pct, info=None):
        self.bars.append((which, pct))


def main() -> int:
    work = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="diskimg-live-",
                                                                       dir=Path.home()))
    work.mkdir(parents=True, exist_ok=True)
    try:
        # ratio text
        check(E.ratio_text(300, 100) == "3.0 : 1  (67 % saved)", E.ratio_text(300, 100))
        check(E.ratio_text(0, 0) == "-", "no data yet: -")
        check(E.ratio_text(100, 110).startswith("none"), E.ratio_text(100, 110))

        # rate meter: drops to 0 while nothing moves
        m = E.RateMeter(window=1.0)
        m.rate(0)
        time.sleep(0.6)
        r1 = m.rate(60 * E.MIB)
        time.sleep(1.2)
        m.rate(60 * E.MIB)
        time.sleep(0.6)
        r2 = m.rate(60 * E.MIB)
        check(r1 > 50 * E.MIB and r2 == 0, f"recent rate {E.human(r1)}/s, then {E.human(r2)}/s when stalled")

        # chunked backup reports written bytes and the data they hold
        src = work / "part.img"
        rnd = random.Random(1)
        with open(src, "wb") as f:
            for i in range(24):
                f.write(rnd.randbytes(E.MIB) if i % 3 == 0 else (b"diskimg " * (E.MIB // 8)))
        calls = []
        st = E.chunked_backup(str(src), [(0, src.stat().st_size)], src.stat().st_size, 3,
                              work / "d.chunks", work / "d.cidx", threading.Event(),
                              lambda *a: calls.append(a))
        last = calls[-1]
        check(len(last) == 5 and last[3] == st["file_bytes"],
              f"final progress: written {E.human(last[3])} = data file {E.human(st['file_bytes'])}")
        check(last[4] == src.stat().st_size, f"data covered {E.human(last[4])}")
        check(st["file_bytes"] < src.stat().st_size / 2,
              f"compression {E.ratio_text(last[4], last[3])}")

        # restore and target check count bytes of data, not chunk numbers
        tgt = work / "restored.img"
        tgt.write_bytes(b"")
        os.truncate(tgt, src.stat().st_size)
        rc = []
        E.chunked_restore(work / "d.chunks", work / "d.cidx", str(tgt), 0, src.stat().st_size,
                          threading.Event(), lambda *a: rc.append(a))
        check(rc[-1] == (src.stat().st_size, src.stat().st_size) and tgt.read_bytes() == src.read_bytes(),
              f"restore progress ends at {E.human(rc[-1][0])} of {E.human(rc[-1][1])}")
        vc = []
        bad = E.chunked_verify_target(work / "d.cidx", str(tgt), 0, threading.Event(), lambda *a: vc.append(a))
        check(not bad and vc[-1] == (src.stat().st_size, src.stat().st_size),
              f"target check progress ends at {E.human(vc[-1][0])} of {E.human(vc[-1][1])}")

        # a partition checked before it is made smaller (inside the copy phase) shows its own
        # 0-100 %, not the copy's total on top (was: "101.2 %" and climbing)
        size = src.stat().st_size
        job = E.Job(mode="i2d", method="fs", src=str(work), dst=str(tgt), src_label="s",
                    dst_label="d", src_dev=None, dst_dev=None, size=size, engine="container")
        sink = Sink()
        r = E.JobRunner(job, sink)
        r.start_phase(2 * size)
        r.w_done = 2 * size                       # everything copied
        it = {"id": "p1", "index_file": "d.cidx", "length": size}
        with r.own_phase(r.item_weight(it)):
            res, _why = r._verify_chunked_target(it, str(tgt), work)
        vb = [p for w, p in sink.bars if w == "verify"]
        check(res is True and vb and max(vb) <= 100.0001 and vb[-1] >= 99.9,
              f"check before shrinking: {len(vb)} updates, {min(vb):.1f} % .. {max(vb):.1f} %")
        check((r.w_done, r.w_total) == (2 * size, 2 * size) and r._live is None,
              "copy phase continues where it was, no stale progress afterwards")
        # the final check counts a partition checked earlier as done
        items = [{"id": "p1", "method": "chunks", "role": "part", "length": size, "_verified": (True, "")},
                 {"id": "p2", "method": "chunks", "role": "part", "length": size, "_verified": (True, "")}]
        res = r.verify_target(job, None, items, {})
        check(res == "ok" and r.w_done == r.w_total, f"final check reaches 100 %: {r.w_done}/{r.w_total}")

        # the runner keeps sending progress (clock running) while the copy is stuck in a write
        job = E.Job(mode="d2i", method="fs", src=str(src), dst=str(work / "x"), src_label="s",
                    dst_label="d", src_dev=None, dst_dev=None, size=1, engine="container",
                    compression=3)
        sink = Sink()
        r = E.JobRunner(job, sink)
        r.running = True
        threading.Thread(target=r._ticker, daemon=True).start()
        prog = r.chunk_progress("p1", 100, "copy")
        r.start_phase(100)
        prog(10 * E.MIB, 100 * E.MIB, 0, 4 * E.MIB, 10 * E.MIB)
        time.sleep(3.2)                     # a long blocking write: no progress calls
        r.running = False
        log = sink.pairs_log
        check(len(log) >= 3, f"{len(log)} updates during 3 s without progress calls")
        els = [p["Elapsed"] for _t, p in log]
        check(len(set(els)) >= 3, f"clock keeps running: {els}")
        p = log[-1][1]
        check(p.get("Written") == E.human(4 * E.MIB) and p.get("Compression", "").startswith("2.5 : 1"),
              f"written {p.get('Written')}, compression {p.get('Compression')}")
        check(p.get("Rate") == "0 B/s", f"rate falls to 0 while stuck: {p.get('Rate')}")
        r.set_step("copy", "done")
        n = len(sink.pairs_log)
        r.running = True
        threading.Thread(target=r._ticker, daemon=True).start()
        time.sleep(1.6)
        r.running = False
        check(len(sink.pairs_log) == n, "no more updates once the step is finished")
    finally:
        if len(sys.argv) <= 1:
            shutil.rmtree(work, ignore_errors=True)
    print("\nLIVE PROGRESS TESTS " + ("PASSED" if ok_all else "FAILED"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
