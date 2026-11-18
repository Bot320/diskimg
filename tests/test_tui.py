# SPDX-License-Identifier: GPL-3.0-only
"""Headless smoke test of the text UI (no root needed; nothing is written to disks).
Run: python3 -I tests/test_tui.py"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diskimg import tui as T  # noqa: E402


async def main() -> int:
    app = T.DiskImgApp()
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause(1.5)
        ms = app.screen
        assert isinstance(ms, T.MainScreen), ms
        print("devices:", len(ms.devs))
        for k in "12345":
            await pilot.press(k)
            await pilot.pause(0.2)
            print(f"mode {ms.mode}: plan ->", end=" ")
            try:
                print(ms.plan().engine)
            except ValueError as e:
                print("needs input:", e)
        await pilot.press("1")
        cb = ms.query_one("#opt-scan")
        cb.value = True
        await pilot.pause(0.4)
        cb.value = False
        await pilot.pause(0.2)
        await pilot.press("n")
        await pilot.pause(0.4)
        print("network dialog:", type(app.screen).__name__)
        await pilot.press("escape")
        await pilot.pause(0.4)
        await pilot.press("6")
        await pilot.pause(1.0)
        print("file backups:", type(app.screen).__name__)
        await pilot.press("escape")
        await pilot.pause(0.4)
        res = {}
        app.push_screen(T.PassphraseModal(new=False, checker=lambda x: (time.sleep(0.8), x == "s3cret!!")[1]),
                        lambda v: res.setdefault("v", v))
        await pilot.pause(0.3)
        app.screen.query_one("#pw1").value = "wrong"
        await pilot.click("#pw-ok")
        t0 = time.monotonic()
        await pilot.pause(0.1)
        responsive = time.monotonic() - t0 < 0.5
        await pilot.pause(1.2)
        app.screen.query_one("#pw1").value = "s3cret!!"
        await pilot.click("#pw-ok")
        await pilot.pause(1.2)
        print("passphrase check off the UI thread:", responsive, "| accepted:", res.get("v"))
        if len(sys.argv) > 1:                      # optional: a .dimg backup to check
            job = T.plan_job(T.PlanInput(mode="chk", src_path=sys.argv[1]), {})
            app.push_screen(T.RunScreen(job))
            for _ in range(120):
                await pilot.pause(0.5)
                if not app.screen.running:
                    break
            rs = app.screen
            print("check job result:", rs.runner.result, "| item rows:", len(rs.items_rows),
                  "| steps:", rs.runner.steps)
            assert rs.runner.result[1] == "$success"
            await pilot.click("#btn-back")
            await pilot.pause(0.5)
        await pilot.pause(3.5)
        print("final screen:", type(app.screen).__name__)
    print("TUI SMOKE TEST OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
