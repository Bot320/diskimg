"""Protocol test of the root helper, run as a normal user in test mode (DISKIMG_HELPER_TEST=1).
Usage: python3 -I tests/test_helper.py [BACKUP.dimg]"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Client:
    def __init__(self):
        env = dict(os.environ, DISKIMG_HELPER_TEST="1")
        self.p = subprocess.Popen([sys.executable, "-I", str(ROOT / "libexec" / "diskimg-helper")],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        self.n = 0
        self.events = []
        first = self.read()
        assert first.get("event") == "ready", first

    def read(self):
        line = self.p.stdout.readline()
        assert line, "helper closed the channel"
        return json.loads(line)          # any non-JSON junk on the channel fails here

    def call(self, cmd, **args):
        self.n += 1
        rid = self.n
        self.p.stdin.write((json.dumps({"id": rid, "cmd": cmd, "args": args}) + "\n").encode())
        self.p.stdin.flush()
        while True:
            m = self.read()
            if m.get("type") == "reply" and m.get("id") == rid:
                if not m["ok"]:
                    raise RuntimeError(m["error"])
                return m["result"]
            self.events.append(m)

    def wait_event(self, name, timeout=300):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            for i, e in enumerate(self.events):
                if e.get("event") == name:
                    return self.events.pop(i)
            self.events.append(self.read())
        raise TimeoutError(name)


def main() -> int:
    c = Client()
    hello = c.call("hello")
    print("hello:", {k: hello[k] for k in ("protocol", "version", "uid")})
    devs = c.call("devices")
    print("devices:", len(devs), "first:", devs[0]["path"] if devs else None)
    print("places:", len(c.call("places")))
    pl = c.call("plan", plan={"mode": "d2i"})
    assert pl["valid"] is False and "source" in pl["message"], pl
    print("plan without source -> invalid:", pl["message"])
    try:
        c.call("no_such_command")
    except RuntimeError as e:
        print("unknown command rejected:", e)
    print("net_list:", c.call("net_list"))
    try:
        c.call("net_parse", url="smb://user:pw@nas/share/sub")
        print("net_parse ok")
    except RuntimeError as e:
        print("net_parse:", e)
    if len(sys.argv) > 1:
        b = sys.argv[1]
        print("path_info:", c.call("path_info", path=b, kind="src")["text"][:80])
        rows = c.call("browse_rows", path=b)
        print("browse rows:", [r["id"] for r in rows["rows"]], "default:", rows["default"])
        prep = c.call("prepare", plan={"mode": "chk", "src_path": b})
        info = c.call("confirm_info", token=prep["token"])
        st = c.call("start", token=prep["token"])
        print("started:", st["engine"], [s[0] for s in st["steps"]])
        assert not [e for e in c.events if e.get("event") == "done"], "events before run"
        c.call("run")
        done = c.wait_event("done")
        kinds = sorted({e.get("event") for e in c.events})
        print("job done:", done["result"], done["style"], "| events seen:", kinds)
        for e in c.events:
            if e.get("event") == "log":
                print("   log:", e["markup"][:150])
        assert done["style"] == "$success", done
        assert info["danger"] is False
        try:
            c.call("start", token=prep["token"])
            print("FAIL: token reused")
            return 1
        except RuntimeError as e:
            print("used token rejected:", e)
    c.p.stdin.close()                        # GUI goes away -> helper cleans up and exits
    rc = c.p.wait(timeout=60)
    print("helper exit code:", rc)
    print("HELPER PROTOCOL TEST OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
