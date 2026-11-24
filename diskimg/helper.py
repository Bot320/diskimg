# SPDX-License-Identifier: GPL-3.0-only
"""Privileged backend for the GTK user interface.

The GUI runs as the desktop user and starts this process with pkexec. They talk over the helper's
stdin/stdout with one JSON object per line:

  request   {"id": 7, "cmd": "devices", "args": {...}}
  reply     {"type": "reply", "id": 7, "ok": true, "result": ...}
            {"type": "reply", "id": 7, "ok": false, "error": "message"}
  event     {"type": "event", "event": "log", ...}           (job progress, usage results, ...)

Only the GUI that started the helper can talk to it (the pipes are private). When the GUI goes
away (end of input) the helper stops a running job, unmounts what it mounted and exits.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import engine as E

PROTOCOL = 1


class Channel:
    """The private JSON-lines channel to the GUI."""

    def __init__(self):
        # Take the protocol pipes away from fd 0/1: child programs (mount, ddrescue, partclone ...)
        # inherit fd 0/1/2 and must never read our requests or write into our replies.
        self.rfd = os.dup(0)
        self.wfd = os.dup(1)
        os.set_inheritable(self.rfd, False)
        os.set_inheritable(self.wfd, False)
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(2, 1)            # stray output of children goes to stderr (the GUI's log)
        os.close(devnull)
        self.rf = os.fdopen(self.rfd, "rb", buffering=0)
        self.wlock = threading.Lock()
        self.closed = threading.Event()

    def send(self, obj: dict) -> None:
        data = (json.dumps(obj, default=str) + "\n").encode()
        with self.wlock:
            if self.closed.is_set():
                return
            try:
                mv = memoryview(data)
                while mv:
                    n = os.write(self.wfd, mv)
                    mv = mv[n:]
            except OSError:
                self.closed.set()

    def event(self, event_name: str, /, **kw) -> None:
        self.send({"type": "event", "event": event_name, **kw})

    def lines(self):
        buf = b""
        while True:
            try:
                chunk = os.read(self.rfd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    yield line


class HelperSink(E.JobSink):
    def __init__(self, ch: Channel):
        self.ch = ch

    def log(self, markup: str) -> None:
        self.ch.event("log", markup=markup)

    def step(self, key: str, state: str) -> None:
        self.ch.event("step", key=key, state=state)

    def item(self, iid: str, cells: dict) -> None:
        self.ch.event("item", id=iid, cells=cells)

    def bar(self, which: str, pct: float, info: str | None = None) -> None:
        self.ch.event("bar", which=which, pct=round(float(pct), 2), info=info)

    def pairs(self, pairs) -> None:
        self.ch.event("pairs", pairs=[[k, v, bool(a)] for k, v, a in pairs])

    def phase(self, text: str) -> None:
        self.ch.event("phase", text=text)

    def button(self, name: str, enabled: bool) -> None:
        self.ch.event("button", name=name, enabled=enabled)

    def begin(self, info: dict) -> None:
        self.ch.event("begin", info=info)


def dev_dict(d: E.Dev) -> dict:
    return {"path": d.path, "name": d.name, "kind": d.kind, "size": d.size, "model": d.model,
            "serial": d.serial, "tran": d.tran, "rm": d.rm, "ro": d.ro, "fstype": d.fstype,
            "label": d.label, "pttype": d.pttype, "mounts": d.mounts, "depth": d.depth,
            "uuid": d.uuid, "busy": d.busy, "parent": d.parent.path if d.parent else None,
            "selectable": E.selectable(d), "desc": d.desc, "long": d.long,
            "has_children": bool(d.children)}


def session_dict(s: E.BrowseSession | None) -> dict | None:
    if s is None:
        return None
    return {"name": s.name, "source": s.source, "root": str(s.root),
            "mounts": [[mp, desc] for mp, desc in s.mounts], "note": s.note,
            "cache": str(s.caches[0].parent) if s.caches else "", "keep": s.keep}


class Helper:
    def __init__(self, ch: Channel):
        self.ch = ch
        self.lock = threading.RLock()
        self.devs: dict[str, E.Dev] = {}
        self.jobs: dict[str, E.Job] = {}          # prepared, not yet started
        self.runner: E.JobRunner | None = None
        self.runner_started = False
        self.job_thread: threading.Thread | None = None
        self.browse: E.BrowseSession | None = None
        self.net_mounted: set[str] = set()
        self.usage_cache: dict[tuple, tuple | None] = {}
        self.usage_inflight: set = set()
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cmd")
        self.ntfs: list[tuple[str, str]] = []

    # ------------------------------------------------------------ dispatch
    def handle(self, raw: bytes) -> None:
        try:
            req = json.loads(raw)
            rid = req.get("id")
            cmd = str(req.get("cmd", ""))
            args = req.get("args") or {}
        except (ValueError, AttributeError):
            self.ch.send({"type": "reply", "id": None, "ok": False, "error": "bad request"})
            return
        fn = getattr(self, "cmd_" + cmd.replace("-", "_"), None)
        if fn is None:
            self.ch.send({"type": "reply", "id": rid, "ok": False, "error": f"unknown command {cmd}"})
            return

        def work():
            try:
                res = fn(**args)
                self.ch.send({"type": "reply", "id": rid, "ok": True, "result": res})
            except (ValueError, E.JobFailed) as e:
                self.ch.send({"type": "reply", "id": rid, "ok": False, "error": str(e)})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.ch.send({"type": "reply", "id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"})
        self.pool.submit(work)

    # ------------------------------------------------------------ general
    def cmd_hello(self) -> dict:
        du = E.desktop_user()
        return {"protocol": PROTOCOL, "version": E.VERSION, "uid": os.geteuid(),
                "desktop_user": du[0] if du else None, "live": Path("/run/archiso").exists(),
                "missing": [t for t in ("ddrescue", "lsblk", "findmnt", "blockdev", "sgdisk", "sfdisk",
                                        "partx", "blkid", "mkswap", "zstd", "gpg") if not shutil.which(t)],
                "restic": bool(shutil.which("restic")), "logdir": str(E.LOG_DIR)}

    def cmd_devices(self, probe_excl: bool = True) -> list[dict]:
        devs = E.load_devices(probe_excl=probe_excl)
        if probe_excl:
            with self.lock:
                self.devs = devs
        return [dev_dict(d) for d in devs.values()]

    def cmd_places(self, extra: list[str] | None = None) -> list[list[str]]:
        return [[a, b] for a, b in E.places_options(extra or [])]

    def cmd_path_info(self, path: str, kind: str = "dst") -> dict:
        """Info line for a source or destination path."""
        raw = path.strip()
        out: dict = {"text": ""}
        if not raw:
            return out
        p = Path(raw)
        if kind == "dst":
            if p.parent.is_dir():
                free = shutil.disk_usage(p.parent).free
                out["text"] = (f"{E.location_text(p.parent)}   free: {E.human(free)}"
                               + ("   [yellow]exists[/]" if p.exists() else ""))
            else:
                out["text"] = "[dim]folder does not exist[/]"
            return out
        if E.is_container(p):
            man = E.read_manifest(p)
            out["text"] = E.container_summary(man) if man else "[b $error]unreadable manifest[/]"
            out["container"] = man is not None
        elif p.is_file():
            parts = [f"raw image, {E.human(p.stat().st_size)} ({p.stat().st_size:,} B)"]
            bad = E.image_unreadable_bytes(str(p))
            if Path(str(p) + ".map").exists():
                parts.append("[green]backup complete[/]" if bad == 0 else
                             f"[yellow]{E.human(bad)} unreadable at backup time (zeros)[/]")
            if Path(str(p) + ".sha256").exists():
                parts.append("checksum file present")
            out["text"] = "   ".join(parts)
        return out

    def cmd_browse_rows(self, path: str) -> dict:
        rows = E.browse_rows_for(path)
        sel = E.default_browse_selection(rows)
        need = sum(r["need"] for r in rows if r["id"] in sel)
        cache = ""
        if path.strip():
            try:
                cache = E.default_cache_dir(Path(path.strip()), need)
            except Exception:  # noqa: BLE001
                cache = ""
        return {"rows": rows, "default": sel, "cache": cache}

    def cmd_cache_info(self, path: str, need: int) -> str:
        if not need:
            return "[dim]nothing needs to be unpacked[/]"
        base = E.existing_ancestor(Path(path or "/"))
        free = shutil.disk_usage(base).free
        return f"needs about {E.human(need)}   free there: {E.human(free)} ({E.location_text(base)})"

    def cmd_default_name(self, src_dev: str, plan: dict) -> str:
        with self.lock:
            d = self.devs.get(src_dev)
        return E.default_backup_name(d, E.wanted_ext(E.PlanInput.from_dict(plan)))

    # ------------------------------------------------------------ usage
    def cmd_usage(self, scan: bool = False) -> bool:
        """Start computing used space (events 'usage'). scan=True also reads unmounted filesystems."""
        gen = E.usage_generation()
        with self.lock:
            devs = list(self.devs.values())
        todo = [d for d in devs if E.selectable(d) and d.fstype != "swap"
                and not (d.children and not d.fstype)]

        def key(d):
            return (d.path, d.size, d.fstype, d.uuid, tuple(d.mounts))

        def work():
            for d in todo:
                k = key(d)
                if not E.usage_allowed(gen):
                    return
                if k in self.usage_cache:
                    v = self.usage_cache[k]
                    self.ch.event("usage", path=d.path, used=v[0] if v else None, total=v[1] if v else None)
                    continue
                if k in self.usage_inflight or (not d.mounts and not scan):
                    continue
                self.usage_inflight.add(k)
                try:
                    v = E.fs_usage(d, gen, scan)
                except Exception:  # noqa: BLE001
                    v = None
                finally:
                    self.usage_inflight.discard(k)
                if not E.usage_allowed(gen):
                    return
                self.usage_cache[k] = v
                self.ch.event("usage", path=d.path, used=v[0] if v else None, total=v[1] if v else None)
        threading.Thread(target=work, daemon=True).start()
        return True

    def cmd_usage_cancel(self) -> bool:
        E.cancel_usage()
        return True

    # ------------------------------------------------------------ planning
    def _plan(self, plan: dict) -> list[E.Job]:
        with self.lock:
            devs = dict(self.devs)
        return E.plan_jobs(E.PlanInput.from_dict(plan), devs)

    def cmd_plan(self, plan: dict) -> dict:
        pi = E.PlanInput.from_dict(plan)
        out = {"engine": E.plan_engine(pi), "ext": E.wanted_ext(pi),
               "label": E.engine_label(pi.mode, pi.method, pi.compression, pi.encrypt,
                                       E.plan_engine(pi) == "container" and pi.mode == "i2d")}
        try:
            jobs = self._plan(plan)
            out.update(valid=True, message=E.batch_summary(jobs), count=len(jobs))
        except ValueError as e:
            out.update(valid=False, message=str(e))
        return out

    def cmd_prepare(self, plan: dict) -> dict:
        jobs = self._plan(plan)
        with self.lock:
            v = {d.path: self.usage_cache.get((d.path, d.size, d.fstype, d.uuid, tuple(d.mounts)))
                 for d in self.devs.values()}
        warnings = []
        for j in jobs:
            warnings += E.prepare_job(j, lambda node: (v.get(node) or (None,))[0])
        token = os.urandom(8).hex()
        with self.lock:
            self.jobs = {token: jobs}            # only the latest prepared job(s) can be started
        j = jobs[0]
        return {"token": token, "warnings": warnings,
                "resume": E.resume_question(j) if len(jobs) == 1 else None,
                "passphrase": E.passphrase_kind(j), "browse": j.engine == "browse", "count": len(jobs)}

    def _jobs(self, token: str) -> list[E.Job]:
        with self.lock:
            j = self.jobs.get(token)
        if j is None:
            raise ValueError("This job is no longer prepared. Press Start again.")
        return j

    def _job(self, token: str) -> E.Job:
        return self._jobs(token)[0]

    def cmd_check_passphrase(self, token: str, passphrase: str) -> bool:
        j = self._job(token)
        if E.passphrase_kind(j) == "existing":
            return E.check_passphrase(j, passphrase)
        return True

    def cmd_confirm_info(self, token: str, resume: str | None = None, passphrase: str | None = None) -> dict:
        jobs = self._jobs(token)
        if resume not in (None, "resume", "fresh"):
            raise ValueError("bad resume choice")
        for j in jobs:
            E.apply_resume_choice(j, resume if len(jobs) == 1 else None)
        kind = E.passphrase_kind(jobs[0])
        if kind:
            if not passphrase:
                raise ValueError("A passphrase is needed.")
            if kind == "new" and len(passphrase) < 8:
                raise ValueError("The passphrase needs at least 8 characters.")
            if kind == "existing" and not E.check_passphrase(jobs[0], passphrase):
                raise ValueError("Wrong passphrase.")
            for j in jobs:
                j.passphrase = passphrase
        j = jobs[0]
        return {"lines": E.confirm_lines_for(jobs), "token_text": E.confirm_token(j) if len(jobs) == 1 else "",
                "danger": j.dst_is_dev}

    # ------------------------------------------------------------ jobs
    def cmd_start(self, token: str, confirm_text: str = "") -> dict:
        with self.lock:
            if self.runner is not None and (self.runner.running or
                                            (self.runner_started and self.job_thread
                                             and self.job_thread.is_alive())):
                raise ValueError("Another job is running.")
            jobs = self.jobs.pop(token, None)
            if jobs is None:
                raise ValueError("This job is no longer prepared. Press Start again.")
            j = jobs[0]
            if len(jobs) == 1 and j.dst_is_dev and confirm_text.strip() != E.confirm_token(j):
                raise ValueError("The confirmation text does not match the target device.")
            if j.engine == "browse" and self.browse is not None:
                raise ValueError("Another backup is open. Close it first.")
            runner = E.make_runner(jobs, HelperSink(self.ch))
            self.runner = runner
            self.runner_started = False
        return runner.start_info()

    def cmd_run(self) -> bool:
        """Begin the job created by 'start'. Separate from 'start' so that no job event can reach
        the GUI before it has shown the progress page."""
        with self.lock:
            runner = self.runner
            if runner is None or self.runner_started:
                raise ValueError("No job to run.")
            self.runner_started = True
        E.block_usage(True)
        self.usage_cache.clear()

        def work():
            try:
                result, style = runner.run()
            except Exception as e:  # noqa: BLE001 - run() catches everything; belt and braces
                result, style = f"ERROR - {e}", "$error"
            finally:
                E.block_usage(False)
            if runner.browse_session is not None:
                with self.lock:
                    self.browse = runner.browse_session
            self.ch.event("done", result=result, style=style, browse=session_dict(self.browse)
                          if runner.browse_session is not None else None)
        t = threading.Thread(target=work, name="job", daemon=True)
        self.job_thread = t
        runner.running = True          # visible as running before the thread gets scheduled
        t.start()
        return True

    def cmd_stop_text(self) -> str | None:
        r = self.runner
        return r.stop_text() if r is not None else None

    def cmd_stop(self) -> bool:
        r = self.runner
        if r is not None and r.running:
            r.request_stop()
            return True
        return False

    def cmd_skip(self) -> bool:
        r = self.runner
        return bool(r is not None and r.running and r.request_skip())

    def cmd_job_running(self) -> bool:
        r = self.runner
        return bool(r is not None and r.running)

    # ------------------------------------------------------------ browse sessions
    def cmd_browse_info(self) -> dict | None:
        with self.lock:
            return session_dict(self.browse)

    def cmd_browse_close(self, force: bool = False) -> list[str]:
        with self.lock:
            s = self.browse
        if s is None:
            return []
        errs = E.close_browse(s, force=force)
        if not errs:
            with self.lock:
                self.browse = None
        return errs

    # ------------------------------------------------------------ network shares
    def cmd_net_list(self) -> dict:
        saved = E.load_shares()
        mounted = {(m.get("source") or "").lower().rstrip("/"): m.get("target") for m in E.net_mounts()}
        rows = []
        for c in saved:
            mp = mounted.pop(E.conn_source(c).lower().rstrip("/"), None)
            rows.append({"key": E.conn_key(c), "conn": {k: v for k, v in c.items() if k != "password"},
                         "has_password": bool(c.get("password")), "mounted": mp})
        for src, mp in mounted.items():
            rows.append({"key": "ext:" + src, "conn": None, "source": src, "mounted": mp})
        return {"rows": rows}

    def cmd_net_parse(self, url: str) -> dict:
        conn, sub = E.parse_net_url(url)
        return {"conn": conn, "sub": sub}

    def cmd_net_connect(self, conn: dict, password: str = "", save: bool = True, savepw: bool = False,
                        use_saved_password: bool = False) -> str:
        conn = {k: str(conn.get(k) or "") for k in ("proto", "server", "share", "user", "domain", "vers")}
        if conn["proto"] not in ("smb", "nfs"):
            raise ValueError("Choose SMB or NFS.")
        if not conn["server"]:
            raise ValueError("Enter the server name or IP address.")
        if not conn["share"]:
            raise ValueError("Enter the share name (SMB) or the export path (NFS).")
        if conn["proto"] == "smb":
            conn["share"] = conn["share"].replace("\\", "/").strip("/")
            if "/" in conn["share"]:
                raise ValueError("SMB: enter only the share name, without sub folders.")
        elif not conn["share"].startswith("/"):
            conn["share"] = "/" + conn["share"]
        conn["vers"] = conn["vers"] or "auto"
        saved = E.load_shares()
        if use_saved_password and not password:
            password = next((s.get("password", "") for s in saved if E.conn_key(s) == E.conn_key(conn)), "")
        already = E.find_mounted(conn)
        mp = E.mount_share(conn, password)
        if not already:
            with self.lock:
                self.net_mounted.add(mp)
        if save:
            entry = dict(conn)
            if savepw and password:
                entry["password"] = password
            elif not savepw:
                entry.pop("password", None)
            saved = [s for s in saved if E.conn_key(s) != E.conn_key(conn)] + [entry]
            E.save_shares(saved)
        return mp

    def cmd_net_disconnect(self, mp: str) -> bool:
        if mp not in {m.get("target") for m in E.net_mounts()}:
            raise ValueError("That is not a connected network share.")
        e = E.umount_path(mp)
        if e:
            raise ValueError(f"Could not disconnect: {e} (is a backup job or file manager using it?)")
        with self.lock:
            self.net_mounted.discard(mp)
        return True

    def cmd_net_forget(self, key: str) -> bool:
        E.save_shares([s for s in E.load_shares() if E.conn_key(s) != key])
        return True

    # ------------------------------------------------------------ file backup sets
    def _set(self, sid: str) -> dict:
        s = next((x for x in E.load_filesets() if x.get("id") == sid), None)
        if s is None:
            raise ValueError("This backup set does not exist (any more).")
        return s

    def cmd_filesets(self) -> dict:
        self.ntfs = E.ntfs_partitions()
        return {"sets": E.load_filesets(), "ntfs": [[a, b] for a, b in self.ntfs],
                "keep": [[k, v[0]] for k, v in E.KEEP_POLICIES.items()],
                "defaults": E.DEFAULT_EXCLUDES, "restic": bool(shutil.which("restic"))}

    def cmd_fileset_save(self, data: dict, old_id: str | None = None) -> dict:
        sets = E.load_filesets()
        old = next((x for x in sets if x.get("id") == old_id), None) if old_id else None
        s = E.validate_fileset(data, old, self.ntfs or E.ntfs_partitions())
        E.save_filesets([x for x in sets if x.get("id") != s["id"]] + [s])
        return s

    def cmd_fileset_delete(self, sid: str) -> bool:
        E.save_filesets([x for x in E.load_filesets() if x.get("id") != sid])
        return True

    def cmd_fileset_info(self, sid: str) -> str:
        return E.fileset_info(self._set(sid))

    def cmd_fileset_precheck(self, sid: str, action: str) -> dict:
        if action not in ("run", "check", "browse"):
            raise ValueError("bad action")
        return E.fileset_precheck(self._set(sid), action)

    def cmd_fileset_password(self, sid: str, password: str) -> bool:
        return E.fileset_set_password(self._set(sid), password)

    def cmd_fileset_browse(self, sid: str) -> dict:
        with self.lock:
            if self.browse is not None:
                raise ValueError("Another backup is open. Close it first.")
        sess, err = E.start_restic_mount(self._set(sid))
        if err:
            raise ValueError(err)
        with self.lock:
            self.browse = sess
        return session_dict(sess)

    def cmd_fileset_prepare(self, sid: str, action: str, read_all: bool = False) -> dict:
        j = E.fileset_job(self._set(sid), "run" if action == "run" else "check", read_all)
        token = os.urandom(8).hex()
        with self.lock:
            self.jobs = {token: [j]}
        return {"token": token}

    # ------------------------------------------------------------ shutdown
    def shutdown(self) -> None:
        r = self.runner
        if r is not None and r.running:
            r.request_quit()
            t = self.job_thread
            if t is not None:
                t.join(timeout=600)
        with self.lock:
            browse = self.browse
            net = set(self.net_mounted)
        E.shutdown_all(browse, net, report=lambda m: print(m, file=sys.stderr))


def main(argv: list[str] | None = None) -> int:
    E.set_c_locale()
    if os.geteuid() != 0 and os.environ.get("DISKIMG_HELPER_TEST") != "1":
        print("diskimg-helper: must run as root (it is started by the diskimg GUI via pkexec).",
              file=sys.stderr)
        return 1
    for d in (E.STATE_DIR, E.MAP_DIR, E.LOG_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    ch = Channel()
    h = Helper(ch)

    import signal

    def on_signal(signum, _frame):
        ch.closed.set()
        threading.Thread(target=lambda: (h.shutdown(), os._exit(128 + signum)), daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)

    ch.event("ready", protocol=PROTOCOL, version=E.VERSION)
    try:
        for line in ch.lines():
            h.handle(line)
    finally:
        # the GUI is gone (closed, crashed or logged out): stop and clean up
        h.shutdown()
        h.pool.shutdown(wait=False, cancel_futures=True)
    return 0
