#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_csh_gui.py  --  Browser GUI to run a .csh script in your shell, with a
                    Cliosoft-SOS lock-check guard for `ihdl` commands.

GOAL
    A general-purpose runner: you pass a .csh file on the command line; the tool
    parses it, and for any `ihdl` (Cadence Verilog-In) command it first checks the
    target schematic/symbol cellviews are NOT locked / checked out by someone else
    in the design-management system (Cliosoft SOS; DesignSync template included).
    If any cellview is blocked it reports the error and STOPS. Otherwise it backs
    up existing log files (so they aren't overwritten), runs the whole .csh in the
    SAME shell environment you launched from, live-tails the log, and estimates
    % complete + ETA by comparing against the previous run's log size.

WHY A LOCAL SERVER (not a static .html)
    A static page can't run shell commands. This script runs a tiny HTTP server IN
    THIS SHELL; the browser talks to localhost, and the .csh / soscmd / ihdl calls
    are subprocesses that inherit os.environ verbatim -- your licenses, PATH and
    module setup carry over with nothing to re-source.

USAGE
    # From a shell where your Cadence / Cliosoft env is set up:
    python3 run_csh_gui.py --csh /path/to/run.csh --open
    # then open the printed URL, e.g. http://127.0.0.1:8988/

No third-party dependencies -- Python 3.5+ standard library only.
"""

import sys

if sys.version_info < (3, 5):
    sys.stderr.write("\n  Requires Python 3.5+ (you are running %s). Use 'python3'.\n\n"
                     % sys.version.split()[0])
    raise SystemExit(1)

import argparse
import getpass
import glob as globmod
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    from http.server import ThreadingHTTPServer                 # Python 3.7+
except ImportError:                                             # 3.5 / 3.6
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True


APP_REVISION = 1        # incremental build number, shown top-right in the GUI


# --------------------------------------------------------------------------- #
#  Configuration  (nothing site-specific is baked in: blanks / env vars, then
#  editable in the Config tab and persisted to a local <config>.json)
# --------------------------------------------------------------------------- #

def _env(name, default=""):
    return os.environ.get(name, default)


DEFAULT_CONFIG = {
    # How to execute the .csh. "auto" -> honour the shebang, else tcsh/csh/sh.
    "csh_shell": _env("CSH_SHELL", "auto"),      # auto | tcsh | csh | sh | bash

    # cds.lib used to resolve a target library name -> on-disk path (for lock
    # checks). Blank -> auto-resolve from the .csh dir / launch dir / $HOME.
    "cds_lib": _env("CDS_LIB", ""),

    # ---- Design-management lock check (default: Cliosoft SOS) ----
    "lock_check": "yes",                          # yes|no  (master switch)
    "dm_system": "sos",                           # sos | designsync | auto
    "ihdl_views": "schematic,symbol",             # cellviews to lock-check

    # Cliosoft SOS: command run per cellview dir; {path}=cellview dir, {user}=login.
    "sos_check_cmd": 'soscmd status -rec "{path}"',
    # A line matching this regex means the view is checked out / locked. If the
    # regex has a capture group it is treated as the owning user (a self-lock is
    # allowed when sos_self_ok=yes).
    "sos_locked_regex": r"(?i)(?:locked|checked[\s-]*out)\s+by\s+(\S+)",
    "sos_self_ok": "yes",                         # allow views you locked yourself

    # DesignSync (used when dm_system=designsync): same {path}/{user} semantics.
    "ds_check_cmd": 'dss ls -report status "{path}"',
    "ds_locked_regex": r"(?i)locked\s+by\s+(\S+)",

    # If the lock-check command errors (tool missing / rc!=0), stop the run?
    "stop_on_lockcheck_error": "yes",             # yes|no

    # Back up existing log files before the run so they aren't overwritten.
    "backup_logs": "yes",

    # Environment / module auto-load (if the .csh's tools aren't on PATH).
    "modules": _env("EDA_MODULES", ""),           # e.g. "cadence/ic618 cliosoft/sos"
    "module_load_cmd": (
        "bash -c '"
        'source "${MODULESHOME:-/usr/share/Modules}/init/bash" 2>/dev/null; '
        "source /etc/profile.d/modules.sh 2>/dev/null; "
        "source /etc/profile.d/lmod.sh 2>/dev/null; "
        "module load {modules} 1>&2; env -0'"),
    "auto_load_modules": "yes",
}

CONFIG_LOCK = threading.Lock()
CONFIG = {}
CONFIG_PATH = None
RUNS_BASE = None
CSH_ARG = ""            # the --csh path passed on the command line


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
            cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
        except Exception as e:
            sys.stderr.write("WARN: could not read config %s: %s\n" % (path, e))
    return cfg


def save_config(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
#  Console output helpers (numbered phase banners, -E- errors, artifacts)
# --------------------------------------------------------------------------- #

def _banner(tag, desc):
    line = "=" * 72
    sys.stdout.write("\n%s\n=====  %s  %s\n%s\n" % (line, tag, desc, line))
    sys.stdout.flush()


def _err(msg):
    for ln in (str(msg).splitlines() or [""]):
        sys.stdout.write("-E- %s\n" % ln)
    sys.stdout.flush()


def _info(msg):
    sys.stdout.write("-I- %s\n" % msg)
    sys.stdout.flush()


def _human_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024.0:
            return "%.0f%s" % (n, u)
        n /= 1024.0
    return "%.1fTB" % n


def _artifact(label, path):
    try:
        st = os.stat(path)
        sys.stdout.write("   -> %-12s %s   (%s, %s)\n" % (
            label + ":", path, _human_size(st.st_size),
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))))
    except OSError:
        sys.stdout.write("   -> %-12s %s   (NOT FOUND)\n" % (label + ":", path))
    sys.stdout.flush()


def _dbg(msg):
    try:
        line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        if RUNS_BASE:
            with open(os.path.join(RUNS_BASE, "gui_debug.log"), "a",
                      encoding="utf-8", errors="replace") as f:
                f.write(line)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  cds.lib resolution + parsing (to map a library name -> on-disk path)
# --------------------------------------------------------------------------- #

def resolve_cds_lib(cfg=None, start_dir=None):
    """Return an EXISTING cds.lib path, or '' -- never a directory/blank artifact.
    Order: configured cds_lib (if a real file) -> the .csh dir and its parents ->
    the launch dir and parents -> $CDS_LIB / $HOME."""
    if cfg is None:
        with CONFIG_LOCK:
            cfg = dict(CONFIG)
    cand = (cfg.get("cds_lib") or "").strip()
    if cand:
        cand = os.path.abspath(os.path.expanduser(os.path.expandvars(cand)))
        if os.path.isfile(cand):
            return cand
    starts = []
    if start_dir:
        starts.append(start_dir)
    if CSH_ARG:
        starts.append(os.path.dirname(os.path.abspath(CSH_ARG)))
    starts.append(os.getcwd())
    for s in starts:
        d = os.path.abspath(s)
        while True:
            fp = os.path.join(d, "cds.lib")
            if os.path.isfile(fp):
                return fp
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    for fp in (os.environ.get("CDS_LIB", ""),
               os.path.join(os.path.expanduser("~"), "cds.lib")):
        if fp:
            fp = os.path.abspath(os.path.expanduser(os.path.expandvars(fp)))
            if os.path.isfile(fp):
                return fp
    return ""


def parse_cds_lib(cds_path, _seen=None):
    """Return {libname: abspath}, following INCLUDE and SOFTINCLUDE."""
    libs = {}
    if _seen is None:
        _seen = set()
    if not cds_path:
        return libs
    cds_path = os.path.abspath(os.path.expanduser(cds_path))
    if cds_path in _seen or not os.path.isfile(cds_path):
        return libs
    _seen.add(cds_path)
    base = os.path.dirname(cds_path)
    try:
        with open(cds_path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = line.split()
                key = parts[0].upper()
                if key == "DEFINE" and len(parts) >= 3:
                    name = parts[1]
                    p = os.path.expandvars(os.path.expanduser(parts[2]))
                    if not os.path.isabs(p):
                        p = os.path.join(base, p)
                    libs.setdefault(name, os.path.normpath(p))
                elif key in ("INCLUDE", "SOFTINCLUDE") and len(parts) >= 2:
                    inc = os.path.expandvars(os.path.expanduser(parts[1]))
                    if not os.path.isabs(inc):
                        inc = os.path.join(base, inc)
                    for n, pth in parse_cds_lib(inc, _seen).items():
                        libs.setdefault(n, pth)
    except Exception as e:
        _dbg("parse_cds_lib %s: %s" % (cds_path, e))
    return libs


# --------------------------------------------------------------------------- #
#  .csh parsing + ihdl target extraction
# --------------------------------------------------------------------------- #

def _read(path, limit=200000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def parse_csh(path):
    """Return {shebang, lines[], commands[]}; each command is {n, text, is_ihdl,
    logs[]}. Continuation lines (trailing backslash) are joined."""
    text = _read(path)
    raw_lines = text.splitlines()
    shebang = raw_lines[0] if raw_lines and raw_lines[0].startswith("#!") else ""
    commands, buf = [], ""
    for ln in raw_lines:
        s = ln.rstrip()
        if not buf and (not s.strip() or s.strip().startswith("#")):
            continue
        if s.endswith("\\"):
            buf += s[:-1] + " "
            continue
        cmd = (buf + s).strip()
        buf = ""
        if not cmd:
            continue
        commands.append(cmd)
    out = []
    for i, cmd in enumerate(commands, 1):
        is_ihdl = bool(re.search(r"(^|[\s;|&/])ihdl(\s|$)", cmd))
        out.append({"n": i, "text": cmd, "is_ihdl": is_ihdl,
                    "logs": _cmd_log_targets(cmd)})
    return {"shebang": shebang, "n_commands": len(out), "commands": out,
            "path": os.path.abspath(path)}


def _cmd_log_targets(cmd):
    """Log files a command writes: redirections (>, >&, >>) and -log <file>."""
    logs = []
    for m in re.finditer(r"(?:>&?>?|\|&?\s*tee(?:\s+-a)?)\s+(\S+)", cmd):
        logs.append(m.group(1))
    m = re.search(r"-log(?:File)?\s+(\S+)", cmd)
    if m:
        logs.append(m.group(1))
    # strip quotes
    return [x.strip('"\'') for x in logs]


def _tokenize(cmd):
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def parse_ihdl(cmd, csh_dir, cfg):
    """Extract the ihdl target library / cells / views / cds.lib / log from an
    ihdl command line, its -param file, any -cdslib, and a fallback .ihdlEnvFile."""
    toks = _tokenize(cmd)
    info = {"cmd": cmd, "library": "", "cells": [], "views": [], "cdslib": "",
            "param": "", "vfiles": [], "log": "", "notes": []}

    def resolve(p):
        p = os.path.expanduser(os.path.expandvars(p))
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(csh_dir, p))

    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if t in ("-param", "-parameters") and nxt:
            info["param"] = resolve(nxt); i += 2; continue
        if t in ("-cdslib", "-cds") and nxt:
            info["cdslib"] = resolve(nxt); i += 2; continue
        if t in ("-log", "-logFile") and nxt:
            info["log"] = resolve(nxt); i += 2; continue
        if t in ("-lib", "-library", "-target", "-targetLibrary") and nxt:
            info["library"] = nxt; i += 2; continue
        if t.lower().endswith((".v", ".vh", ".sv", ".va")):
            info["vfiles"].append(resolve(t))
        i += 1

    # pull settings from the -param file (ihdl / .ihdlEnvFile "Key =Value" style)
    for src in [info["param"]]:
        if src and os.path.isfile(src):
            _apply_ihdl_env(_read(src), info, resolve)
    # fallback: a stray .ihdlEnvFile next to the .csh or in $HOME
    if not info["library"] or not info["views"]:
        for envf in (os.path.join(csh_dir, ".ihdlEnvFile"),
                     os.path.join(os.path.expanduser("~"), ".ihdlEnvFile")):
            if os.path.isfile(envf):
                _apply_ihdl_env(_read(envf), info, resolve)
                info["notes"].append("used %s for defaults" % envf)
                break

    # views: default from config if none discovered
    if not info["views"]:
        info["views"] = [v.strip() for v in cfg.get("ihdl_views", "schematic,symbol").split(",") if v.strip()]

    # cells: top module name(s) from the verilog design files
    if not info["cells"]:
        for vf in info["vfiles"]:
            for m in re.finditer(r"^\s*module\s+([A-Za-z_]\w*)", _read(vf), re.M):
                if m.group(1) not in info["cells"]:
                    info["cells"].append(m.group(1))
    # a `# LOCKCHECK lib= cell= views=` directive in the .csh always wins
    return info


def _apply_ihdl_env(text, info, resolve):
    def val(key):
        m = re.search(r"^\s*%s\s*=\s*(.+?)\s*$" % re.escape(key), text, re.M | re.I)
        return m.group(1).strip() if m else ""
    if not info["library"]:
        info["library"] = val("Target Library") or val("library") or val("targetLib")
    dv = [("Schematic View Name", "schematic"), ("Symbol View Name", "symbol")]
    for key, _ in dv:
        v = val(key)
        if v and v not in info["views"]:
            info["views"].append(v)
    lf = val("Log File")
    if lf and not info["log"]:
        info["log"] = resolve(lf)
    df = val("Verilog Design Files")
    for f in df.split():
        rf = resolve(f)
        if rf not in info["vfiles"]:
            info["vfiles"].append(rf)
    cell = val("Cell") or val("Top Cell") or val("topCell")
    if cell and cell not in info["cells"]:
        info["cells"].append(cell)


def _lockcheck_overrides(csh_text):
    """`# LOCKCHECK lib=.. cell=.. views=a,b` directives in the .csh."""
    outs = []
    for m in re.finditer(r"#\s*LOCKCHECK\s+(.+)$", csh_text, re.M | re.I):
        kv = dict(re.findall(r"(\w+)\s*=\s*(\S+)", m.group(1)))
        outs.append({"library": kv.get("lib", ""), "cells": [c for c in kv.get("cell", "").split(",") if c],
                     "views": [v for v in kv.get("views", "").split(",") if v]})
    return outs


def cellview_dirs(info, cfg, csh_dir):
    """Resolve <libpath>/<cell>/<view> dirs to lock-check, via cds.lib."""
    cds = info["cdslib"] or resolve_cds_lib(cfg, csh_dir)
    libs = parse_cds_lib(cds)
    libpath = libs.get(info["library"], "")
    out = []
    for cell in (info["cells"] or []):
        for view in (info["views"] or []):
            d = os.path.join(libpath, cell, view) if libpath else ""
            out.append({"lib": info["library"], "cell": cell, "view": view,
                        "path": d, "exists": bool(d) and os.path.isdir(d)})
    return {"cds_lib": cds, "lib_path": libpath, "cellviews": out}


# --------------------------------------------------------------------------- #
#  Cliosoft SOS / DesignSync lock check
# --------------------------------------------------------------------------- #

def lock_check_one(path, cfg):
    """Run the DM lock-check command on a cellview dir; classify the result."""
    user = getpass.getuser()
    if cfg.get("dm_system", "sos") == "designsync":
        tmpl, rx = cfg["ds_check_cmd"], cfg["ds_locked_regex"]
    else:
        tmpl, rx = cfg["sos_check_cmd"], cfg["sos_locked_regex"]
    cmd = tmpl.replace("{path}", path).replace("{user}", user)
    res = {"path": path, "cmd": cmd, "blocked": False, "status": "clean",
           "owner": "", "output": "", "rc": None}
    try:
        proc = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120)
        out = proc.stdout.decode("utf-8", "replace")
        res["rc"] = proc.returncode
        res["output"] = out[-4000:]
    except FileNotFoundError:
        res["status"] = "tool-missing"; return res
    except Exception as e:
        res["status"] = "error"; res["output"] = str(e); return res

    if res["rc"] != 0:
        res["status"] = "error"
        # still scan output; a nonzero rc alone is decided by caller policy
    self_ok = str(cfg.get("sos_self_ok", "yes")).strip().lower() in ("1", "yes", "true", "on")
    for line in out.splitlines():
        m = re.search(rx, line)
        if m:
            owner = m.group(1) if m.groups() else ""
            if owner and self_ok and owner == user:
                res["status"] = "self-lock"; res["owner"] = owner
                continue
            res["blocked"] = True
            res["status"] = "locked"
            res["owner"] = owner
            break
    return res


# --------------------------------------------------------------------------- #
#  Environment / module auto-load
# --------------------------------------------------------------------------- #

def load_modules(modules_str, cfg):
    modules_str = (modules_str or cfg.get("modules", "")).strip()
    if not modules_str:
        return {"ok": True, "applied": 0}
    cmd = cfg.get("module_load_cmd", "").replace("{modules}", modules_str)
    try:
        proc = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=180)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    applied = 0
    raw = proc.stdout
    if b"=" in raw:
        parts = raw.split(b"\0") if b"\0" in raw else raw.split(b"\n")
        newenv = {}
        for chunk in parts:
            if b"=" in chunk:
                k, v = chunk.split(b"=", 1)
                try:
                    newenv[k.decode()] = v.decode()
                except Exception:
                    pass
        if "PATH" in newenv:
            for k, v in newenv.items():
                if os.environ.get(k) != v:
                    applied += 1
            os.environ.update(newenv)
    return {"ok": True, "applied": applied,
            "stderr": proc.stderr.decode("utf-8", "replace")[-4000:]}


# --------------------------------------------------------------------------- #
#  Job management
# --------------------------------------------------------------------------- #

class Job(object):
    def __init__(self, job_id, meta):
        self.id = job_id
        self.meta = meta
        self.state = "queued"
        self.steps = []
        self.log_path = os.path.join(meta["run_dir"], "run.log")
        self.primary_log = meta.get("primary_log") or self.log_path
        self.baseline_bytes = 0
        self.result = None
        self.error = None
        self.started = time.time()
        self.finished = None
        self.proc = None
        self.lock_report = []
        self._lock = threading.Lock()

    def stop(self):
        self.meta["stop_requested"] = True
        p = self.proc
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
        return p is not None

    def snapshot(self, tail=200000):
        with self._lock:
            data = {"id": self.id, "state": self.state, "steps": list(self.steps),
                    "meta": {k: v for k, v in self.meta.items() if k != "cfg_snapshot"},
                    "error": self.error, "result": self.result,
                    "started": self.started, "finished": self.finished,
                    "lock_report": list(self.lock_report)}
        # live log = the primary log if it exists, else the captured run.log
        logf = self.primary_log if os.path.isfile(self.primary_log) else self.log_path
        log, size = "", 0
        try:
            if os.path.isfile(logf):
                size = os.path.getsize(logf)
                with open(logf, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(max(0, size - tail))
                    log = f.read()
        except Exception:
            pass
        data["log"] = log
        data["log_file"] = logf
        elapsed = (self.finished or time.time()) - self.started
        data["elapsed"] = elapsed
        base = self.baseline_bytes or 0
        if self.state == "done":
            data["progress"] = 100
        elif self.state == "failed":
            data["progress"] = None
        elif base > 0 and size > 0:
            data["progress"] = min(99, int(size * 100.0 / base))
            rate = size / elapsed if elapsed > 0 else 0
            data["eta"] = max(0, (base - size) / rate) if (rate > 0 and base > size) else 0
        else:
            data["progress"] = None
        data["baseline_bytes"] = base
        return data


JOBS = {}
JOBS_LOCK = threading.Lock()
_JOB_COUNTER = [0]


def _log(job, msg):
    with open(job.log_path, "a", encoding="utf-8", errors="replace") as f:
        f.write(msg)


def _run_step(job, name, cmd_list_or_str, cwd, shell=False):
    with job._lock:
        step = {"name": name,
                "cmd": cmd_list_or_str if shell else " ".join(shlex.quote(c) for c in cmd_list_or_str),
                "rc": None, "state": "running"}
        job.steps.append(step)
        num = len(job.steps)
    _log(job, "\n" + "=" * 78 + "\n### STEP %d: %s\n### CMD: %s\n### CWD: %s\n"
         % (num, name, step["cmd"], cwd) + "=" * 78 + "\n")
    sys.stdout.write("\n------ command used for %s ------\n  cd %s && %s\n"
                     % (name, shlex.quote(cwd), step["cmd"]))
    sys.stdout.flush()
    try:
        proc = subprocess.Popen(cmd_list_or_str, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=1, shell=shell,
                                encoding="utf-8", errors="replace", env=os.environ)
    except FileNotFoundError as e:
        _log(job, "!! not found: %s\n" % e)
        step["state"] = "failed"; step["rc"] = 127
        sys.stdout.write("------ %s: FAILED (rc=127, not found) ------\n" % name)
        sys.stdout.flush()
        return 127
    job.proc = proc
    stop_hb = threading.Event()

    def _heartbeat():
        while not stop_hb.wait(5.0):
            try:
                logf = job.primary_log if os.path.isfile(job.primary_log) else job.log_path
                if os.path.isfile(logf):
                    st = os.stat(logf)
                    sys.stdout.write("       ...running (%s): %s = %s, updated %ds ago\n" %
                                     (name, os.path.basename(logf), _human_size(st.st_size),
                                      int(time.time() - st.st_mtime)))
                    sys.stdout.flush()
            except Exception:
                pass
    hb = threading.Thread(target=_heartbeat, daemon=True); hb.start()
    tail = []
    try:
        with open(job.log_path, "a", encoding="utf-8", errors="replace") as lf:
            for line in iter(proc.stdout.readline, ""):
                lf.write(line); lf.flush()
                tail.append(line)
                if len(tail) > 60:
                    del tail[0]
            proc.stdout.close()
        rc = proc.wait()
    finally:
        stop_hb.set(); job.proc = None
    step["rc"] = rc
    step["state"] = "done" if rc == 0 else "failed"
    _log(job, "\n### STEP %d %s finished rc=%d\n" % (num, name, rc))
    sys.stdout.write("------ %s: %s (rc=%s) ------\n" % (name, "OK" if rc == 0 else "FAILED", rc))
    if rc != 0:
        sys.stdout.write("-E- ----- last output of '%s' (rc=%d) -----\n" % (name, rc))
        for ln in tail:
            sys.stdout.write("-E- %s" % (ln if ln.endswith("\n") else ln + "\n"))
    sys.stdout.flush()
    return rc


def _find_baseline(primary_log, run_dir):
    """Baseline bytes for ETA: a prior run.log of the same .csh, or a backed-up
    copy of the primary log."""
    best = 0
    for name in sorted(os.listdir(RUNS_BASE), reverse=True):
        rp = os.path.join(RUNS_BASE, name, "run.log")
        if os.path.dirname(os.path.dirname(rp)) == RUNS_BASE and os.path.isfile(rp) \
                and os.path.abspath(rp) != os.path.abspath(os.path.join(run_dir, "run.log")):
            best = os.path.getsize(rp)
            break
    # also consider the pre-run size of the primary log (the previous run's output)
    return best


def _backup_logs(job, log_paths):
    """Copy existing log files to timestamped .bak so the run won't overwrite them.
    Returns the largest pre-existing size (used as an ETA baseline)."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    biggest = 0
    for lp in dict.fromkeys(p for p in log_paths if p):
        if os.path.isfile(lp):
            try:
                sz = os.path.getsize(lp)
                bak = "%s.%s.bak" % (lp, ts)
                shutil.copy2(lp, bak)
                _artifact("backed up", bak)
                biggest = max(biggest, sz)
            except OSError as e:
                _err("could not back up %s: %s" % (lp, e))
    return biggest


def run_job(job):
    cfg = job.meta["cfg_snapshot"]
    run_dir = job.meta["run_dir"]
    csh = job.meta["csh"]
    csh_dir = os.path.dirname(csh)
    job.state = "running"
    step_no = [0]

    def phase(desc):
        step_no[0] += 1
        _banner("STEP %d:" % step_no[0], desc)

    try:
        _banner("JOB START:", "run %s   (run dir: %s)" % (csh, run_dir))

        # --- 1. parse the .csh ---
        phase("Parse .csh script")
        if not os.path.isfile(csh):
            raise RuntimeError("csh file not found: %s" % csh)
        parsed = parse_csh(csh)
        ihdls = [c for c in parsed["commands"] if c["is_ihdl"]]
        sys.stdout.write("   %d command(s), %d ihdl command(s)\n"
                         % (parsed["n_commands"], len(ihdls)))
        for c in parsed["commands"]:
            sys.stdout.write("     %2d.%s %s\n" % (c["n"], " [ihdl]" if c["is_ihdl"] else "      ",
                                                   c["text"][:120]))
        sys.stdout.flush()

        # collect the log files the script writes (for backup + live view)
        all_logs = []
        for c in parsed["commands"]:
            for lg in c["logs"]:
                p = lg if os.path.isabs(lg) else os.path.normpath(os.path.join(csh_dir, lg))
                all_logs.append(p)

        # --- 2. environment ---
        phase("Check environment / module-load")
        interp = _resolve_interp(cfg, parsed["shebang"], csh)
        sys.stdout.write("   interpreter: %s\n" % interp)
        if not shutil.which(interp.split()[0]) and not os.path.isabs(interp.split()[0]):
            if str(cfg.get("auto_load_modules", "yes")).lower() in ("1", "yes", "true", "on") \
                    and cfg.get("modules", "").strip():
                r = load_modules("", cfg)
                sys.stdout.write("   module load applied %s vars\n" % r.get("applied"))
        sys.stdout.flush()

        # --- 3. Cliosoft SOS lock-check for ihdl targets ---
        do_lock = str(cfg.get("lock_check", "yes")).strip().lower() in ("1", "yes", "true", "on")
        if ihdls and do_lock:
            phase("Lock check (%s) for ihdl target cellviews" % cfg.get("dm_system", "sos").upper())
            overrides = _lockcheck_overrides(_read(csh))
            blocked_any = False
            for c in ihdls:
                info = parse_ihdl(c["text"], csh_dir, cfg)
                for ov in overrides:                 # `# LOCKCHECK` directive wins
                    if ov["library"]:
                        info["library"] = ov["library"]
                    if ov["cells"]:
                        info["cells"] = ov["cells"]
                    if ov["views"]:
                        info["views"] = ov["views"]
                cv = cellview_dirs(info, cfg, csh_dir)
                sys.stdout.write("   ihdl: lib=%s cells=%s views=%s\n" %
                                 (info["library"] or "?", ",".join(info["cells"]) or "?",
                                  ",".join(info["views"]) or "?"))
                if not info["library"] or not info["cells"]:
                    _err("could not determine ihdl target (library/cell). "
                         "Add a '# LOCKCHECK lib=.. cell=.. views=..' line to the .csh.")
                    if str(cfg.get("stop_on_lockcheck_error", "yes")).lower() in ("1", "yes", "true", "on"):
                        raise RuntimeError("lock-check target unknown; stopping (see -E- above)")
                for view in cv["cellviews"]:
                    if not view["exists"]:
                        rec = {"path": view["path"] or "(unresolved)", "status": "no-cellview",
                               "blocked": False, "cell": view["cell"], "view": view["view"]}
                        sys.stdout.write("     %-9s %s/%s -> not on disk yet (will be created)\n"
                                         % ("[new]", view["cell"], view["view"]))
                    else:
                        rec = lock_check_one(view["path"], cfg)
                        rec["cell"] = view["cell"]; rec["view"] = view["view"]
                        mark = {"clean": "[ ok ]", "self-lock": "[self]", "locked": "[LOCK]",
                                "tool-missing": "[????]", "error": "[err ]",
                                "no-cellview": "[new ]"}.get(rec["status"], "[????]")
                        sys.stdout.write("     %-9s %s/%s -> %s%s\n" %
                                         (mark, rec["cell"], rec["view"], rec["status"],
                                          (" by %s" % rec["owner"]) if rec.get("owner") else ""))
                        if rec["status"] in ("tool-missing", "error") and \
                                str(cfg.get("stop_on_lockcheck_error", "yes")).lower() in ("1", "yes", "true", "on"):
                            blocked_any = True
                        if rec["blocked"]:
                            blocked_any = True
                            _err("BLOCKED: %s/%s is %s%s -- release it or check it in, then retry."
                                 % (rec["cell"], rec["view"], rec["status"],
                                    (" by %s" % rec["owner"]) if rec.get("owner") else ""))
                    with job._lock:
                        job.lock_report.append(rec)
                    sys.stdout.flush()
            if blocked_any:
                raise RuntimeError("lock check failed -- not running the .csh (see -E- lines)")
        elif ihdls:
            phase("Lock check SKIPPED (lock_check=no) -- %d ihdl command(s)" % len(ihdls))

        # --- 4. back up existing logs so they aren't overwritten ---
        if str(cfg.get("backup_logs", "yes")).lower() in ("1", "yes", "true", "on"):
            phase("Back up existing log files")
            pre_size = _backup_logs(job, all_logs)
        else:
            pre_size = 0

        # choose the primary log to live-tail + baseline for ETA
        primary = all_logs[0] if all_logs else job.log_path
        job.primary_log = primary
        job.baseline_bytes = pre_size or _find_baseline(primary, run_dir)
        sys.stdout.write("   live log: %s   (ETA baseline %s)\n"
                         % (primary, _human_size(job.baseline_bytes)))
        sys.stdout.flush()

        # --- 5. run the whole .csh in the shell (env inherited) ---
        phase("Run .csh (%s) -- live log below" % interp)
        cmd = "%s %s" % (interp, shlex.quote(csh))
        rc = _run_step(job, "run .csh", cmd, csh_dir, shell=True)

        # --- 6. summarise ---
        phase("Result")
        job.result = _summarize(job, csh, ihdls, rc)
        meta_out = {k: v for k, v in job.meta.items() if k != "cfg_snapshot"}
        meta_out["result"] = job.result
        meta_out["finished"] = time.time()
        meta_out["primary_log"] = primary
        with open(os.path.join(run_dir, "metadata.json"), "w", encoding="utf-8") as f:
            json.dump(meta_out, f, indent=2, default=str)
        if rc != 0:
            raise RuntimeError(".csh exited rc=%d" % rc)
        job.state = "done"
        _banner("JOB DONE:", "%s -> %s" % (os.path.basename(csh), job.result.get("status")))
    except Exception as e:
        job.error = str(e)
        job.state = "failed"
        _log(job, "\n!!! JOB FAILED: %s\n%s\n" % (e, traceback.format_exc()))
        _banner("JOB FAILED:", os.path.basename(csh))
        _err(str(e))
    finally:
        job.finished = time.time()


def _resolve_interp(cfg, shebang, csh):
    pref = (cfg.get("csh_shell") or "auto").strip()
    if pref != "auto":
        return pref
    if shebang.startswith("#!"):
        return shebang[2:].strip()
    if csh.endswith(".csh"):
        return shutil.which("tcsh") and "tcsh" or "csh"
    return "sh"


def _summarize(job, csh, ihdls, rc):
    text = ""
    try:
        logf = job.primary_log if os.path.isfile(job.primary_log) else job.log_path
        with open(logf, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        pass
    errors = len(re.findall(r"^\s*\*?ERROR", text, re.M | re.I)) + \
        len(re.findall(r"VERILOGIN-\d+.*error", text, re.I))
    created = bool(re.search(r"VERILOGIN-198|Creating schematic|VERILOGIN-264.*Done", text))
    status = "OK" if rc == 0 and errors == 0 else ("ERRORS" if errors else ("FAILED" if rc else "OK"))
    return {"type": "csh", "status": status, "rc": rc, "errors": errors,
            "ihdl": len(ihdls), "schematic_created": created,
            "cell": os.path.basename(csh)}


def start_job(meta):
    with JOBS_LOCK:
        _JOB_COUNTER[0] += 1
        job_id = "job%d_%d" % (_JOB_COUNTER[0], int(time.time()))
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", "%s_%s" % (os.path.basename(meta["csh"]), ts))
    run_dir = os.path.join(RUNS_BASE, name)
    os.makedirs(run_dir, exist_ok=True)
    meta["run_dir"] = run_dir
    job = Job(job_id, meta)
    with JOBS_LOCK:
        JOBS[job_id] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


def list_runs():
    runs = []
    if not os.path.isdir(RUNS_BASE):
        return runs
    for name in sorted(os.listdir(RUNS_BASE), reverse=True):
        mp = os.path.join(RUNS_BASE, name, "metadata.json")
        if os.path.isfile(mp):
            try:
                with open(mp, encoding="utf-8") as f:
                    m = json.load(f)
                runs.append({"run_name": name, "csh": m.get("csh"),
                             "status": (m.get("result") or {}).get("status", "?"),
                             "finished": m.get("finished")})
            except Exception:
                pass
    return runs


# --------------------------------------------------------------------------- #
#  HTTP server
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "RunCshGUI/1.0"
    _DISCONNECT = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except self._DISCONNECT:
            self.close_connection = True

    def _write(self, body, code, ctype):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except self._DISCONNECT:
            self.close_connection = True

    def _json(self, obj, code=200):
        self._write(json.dumps(obj, default=str).encode("utf-8"), code, "application/json")

    def _html(self, text, code=200):
        self._write(text.encode("utf-8"), code, "text/html; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path); path = u.path; q = parse_qs(u.query)
        try:
            if path == "/favicon.ico":
                return self._write(b"", 204, "image/x-icon")
            if path in ("/", "/index.html"):
                return self._html(INDEX_HTML.replace("__REV__", str(APP_REVISION)))
            if path == "/api/config":
                with CONFIG_LOCK:
                    return self._json(dict(CONFIG))
            if path == "/api/startup":
                return self._json({"csh": os.path.abspath(CSH_ARG) if CSH_ARG else ""})
            if path == "/api/parse":
                p = q.get("csh", [""])[0] or CSH_ARG
                return self._json(self._parse_preview(p))
            if path == "/api/runs":
                return self._json({"runs": list_runs()})
            if path == "/api/job":
                job = JOBS.get(q.get("id", [""])[0])
                if not job:
                    return self._json({"error": "no such job"}, 404)
                return self._json(job.snapshot())
            if path == "/api/debuglog":
                dl = os.path.join(RUNS_BASE, "gui_debug.log"); txt = ""
                if os.path.isfile(dl):
                    with open(dl, encoding="utf-8", errors="replace") as f:
                        txt = f.read()[-20000:]
                return self._json({"path": dl, "text": txt})
            return self._json({"error": "not found"}, 404)
        except self._DISCONNECT:
            self.close_connection = True
        except Exception as e:
            return self._json({"error": str(e), "trace": traceback.format_exc()}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/run":
                csh = (body.get("csh") or CSH_ARG or "").strip()
                if not csh or not os.path.isfile(csh):
                    return self._json({"error": "csh file not found: %r" % csh}, 400)
                with CONFIG_LOCK:
                    cfg = dict(CONFIG)
                job = start_job({"csh": os.path.abspath(csh), "cfg_snapshot": cfg})
                return self._json({"job_id": job.id, "run_dir": job.meta["run_dir"]})
            if path == "/api/stop":
                job = JOBS.get(body.get("job_id", ""))
                if not job:
                    return self._json({"error": "no such job"}, 404)
                return self._json({"ok": True, "killed": job.stop()})
            if path == "/api/config":
                with CONFIG_LOCK:
                    for k, v in body.items():
                        if k in DEFAULT_CONFIG:
                            CONFIG[k] = v
                    save_config(CONFIG_PATH, CONFIG)
                    return self._json({"ok": True, "config": dict(CONFIG)})
            return self._json({"error": "not found"}, 404)
        except self._DISCONNECT:
            self.close_connection = True
        except Exception as e:
            return self._json({"error": str(e), "trace": traceback.format_exc()}, 500)

    def _parse_preview(self, csh):
        """Parse a .csh and, for each ihdl command, resolve its lock-check targets
        (without running anything) so the GUI can preview before GO."""
        if not csh or not os.path.isfile(csh):
            return {"error": "csh not found: %r" % csh}
        with CONFIG_LOCK:
            cfg = dict(CONFIG)
        parsed = parse_csh(csh)
        csh_dir = os.path.dirname(os.path.abspath(csh))
        overrides = _lockcheck_overrides(_read(csh))
        ihdl_targets = []
        for c in parsed["commands"]:
            if not c["is_ihdl"]:
                continue
            info = parse_ihdl(c["text"], csh_dir, cfg)
            for ov in overrides:
                if ov["library"]:
                    info["library"] = ov["library"]
                if ov["cells"]:
                    info["cells"] = ov["cells"]
                if ov["views"]:
                    info["views"] = ov["views"]
            cv = cellview_dirs(info, cfg, csh_dir)
            ihdl_targets.append({"n": c["n"], "library": info["library"],
                                 "cells": info["cells"], "views": info["views"],
                                 "cds_lib": cv["cds_lib"], "cellviews": cv["cellviews"],
                                 "notes": info["notes"]})
        return {"path": parsed["path"], "shebang": parsed["shebang"],
                "commands": parsed["commands"], "ihdl_targets": ihdl_targets,
                "dm_system": cfg.get("dm_system", "sos"),
                "lock_check": cfg.get("lock_check", "yes")}


# --------------------------------------------------------------------------- #
#  Front-end
# --------------------------------------------------------------------------- #

INDEX_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Run .csh + ihdl lock-check</title>
<style>
 :root{--bg:#F4F5F7;--panel:#FFFFFF;--panel2:#F4F5F7;--fg:#172B4D;--muted:#5E6C84;
       --acc:#0052CC;--acc-dark:#003D99;--acc-light:#DEEBFF;--acc-lighter:#F4F8FF;
       --good:#00875A;--good-bg:#E3FCEF;--bad:#DE350B;--bad-bg:#FFEBE6;
       --warn:#FF8B00;--warn-bg:#FFF7E6;--grey:#97A0AF;--line:#DFE1E6;}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
 header{background:linear-gradient(135deg,var(--acc),var(--acc-dark));color:#fff;
        padding:14px 22px;display:flex;align-items:center;gap:16px}
 header h1{font-size:16px;margin:0;font-weight:600;color:#fff}
 header .sub{color:rgba(255,255,255,.85);font-size:12px}
 .tabs{display:flex;gap:2px;padding:0 18px;background:var(--panel);border-bottom:2px solid var(--line)}
 .tab{padding:11px 18px;cursor:pointer;color:var(--muted);font-weight:500;border-bottom:2px solid transparent;margin-bottom:-2px}
 .tab:hover{color:var(--acc)} .tab.active{color:var(--acc);border-bottom-color:var(--acc)}
 main{padding:22px 18px;max-width:1150px;margin:0 auto}
 .panel{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:18px;margin-bottom:16px;box-shadow:0 1px 2px rgba(9,30,66,.08)}
 .panel h2{margin:0 0 12px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:700}
 label{display:block;font-size:12px;color:var(--muted);margin:8px 0 3px;font-weight:500}
 input[type=text],textarea,select{width:100%;background:#fff;color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:8px 10px;font:inherit}
 input:focus,textarea:focus,select:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 2px var(--acc-light)}
 textarea{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
 .row{display:flex;gap:12px;flex-wrap:wrap}.row>div{flex:1;min-width:180px}
 button{background:var(--acc);color:#fff;border:0;border-radius:4px;padding:9px 18px;font-weight:600;cursor:pointer;font:inherit}
 button:hover{background:var(--acc-dark)} button.sec{background:#fff;color:var(--acc);border:1px solid var(--line)}
 button:disabled{opacity:.5;cursor:default}
 .gobtn{width:104px;height:104px;border-radius:50%;border:0;cursor:pointer;color:#fff;font-size:30px;font-weight:800;letter-spacing:2px;
        background:radial-gradient(circle at 38% 34%,#12c99b,#0a8f6f 60%,#087a5e);box-shadow:0 5px 16px rgba(10,143,111,.45)}
 .gobtn:hover{filter:brightness(1.07)} .gobtn:active{transform:scale(.96)} .gobtn:disabled{opacity:.5;filter:grayscale(.3)}
 pre{background:#0b0f13;color:#d7e0e8;border:1px solid var(--line);border-radius:6px;padding:12px;overflow:auto;
     font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;max-height:460px;white-space:pre-wrap;word-break:break-word}
 table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}
 th,td{border:1px solid var(--line);padding:6px 10px;text-align:left}
 th{background:var(--acc-light);color:var(--fg);font-weight:600}
 .pill{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;font-weight:600}
 .pill.good{background:var(--good-bg);color:var(--good)} .pill.bad{background:var(--bad-bg);color:var(--bad)}
 .pill.warn{background:var(--warn-bg);color:var(--warn)} .pill.muted{background:var(--panel2);color:var(--muted)}
 .muted{color:var(--muted)} .hidden{display:none}
 .spinner{display:inline-block;width:13px;height:13px;border:2px solid var(--line);border-top-color:var(--acc);border-radius:50%;animation:spin .7s linear infinite;vertical-align:-2px;margin-right:5px}
 @keyframes spin{to{transform:rotate(360deg)}}
 .progbar{height:12px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;overflow:hidden;position:relative}
 .progfill{height:100%;width:0;border-radius:6px;background:linear-gradient(90deg,var(--acc),var(--acc-dark));transition:width .5s}
 .progfill.indet{width:35%;position:absolute;animation:indet 1.2s infinite ease-in-out}
 @keyframes indet{0%{left:-35%}100%{left:100%}} .progfill.good{background:var(--good)} .progfill.bad{background:var(--bad)}
 code{background:var(--panel2);padding:1px 5px;border-radius:3px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.92em}
</style></head>
<body>
<header>
  <h1>Run&nbsp;.csh&nbsp;+&nbsp;ihdl&nbsp;lock-check</h1>
  <span class="sub">runs in your launching shell &mdash; env inherited</span>
  <span style="margin-left:auto;font-weight:700;font-size:13px;background:rgba(255,255,255,.2);padding:5px 13px;border-radius:14px">rev __REV__</span>
</header>
<div class="tabs">
  <div class="tab active" data-tab="run">Run</div>
  <div class="tab" data-tab="history">History</div>
  <div class="tab" data-tab="config">Config</div>
</div>
<main>

<section id="tab-run">
  <div class="panel" style="border:2px solid var(--acc)">
    <h2>Run a .csh script</h2>
    <div class="row">
      <div style="flex:4"><label>Path to .csh (passed on the command line, or edit here)</label>
        <input type="text" id="cshpath" placeholder="/path/to/run.csh"></div>
      <div style="flex:0 0 auto;display:flex;align-items:flex-end;gap:10px">
        <button class="sec" id="parsebtn">Preview</button>
        <button class="gobtn" id="gobtn">GO</button></div>
    </div>
    <div id="runmsg" class="muted" style="margin-top:8px;font-size:12px"></div>
  </div>

  <div class="panel" id="previewpanel" style="display:none">
    <h2>Preview &mdash; commands &amp; ihdl lock-check targets</h2>
    <div id="previewbody"></div>
  </div>

  <div class="panel" id="livepanel" style="display:none">
    <h2>Run status &nbsp;<span id="jobstate" class="pill muted"></span>
      <button class="sec" id="stopbtn" style="float:right;padding:4px 14px;color:var(--bad);border-color:var(--bad);display:none">&#9632; Stop</button></h2>
    <div style="display:flex;align-items:center;gap:14px">
      <div class="progbar" style="flex:1"><div id="progfill" class="progfill"></div></div>
      <div id="progpct" style="font-size:24px;font-weight:800;min-width:96px;text-align:right"></div>
    </div>
    <div id="progtext" style="font-size:15px;margin-top:7px"></div>
    <div id="lockbox" style="margin:12px 0"></div>
    <div id="steps" style="margin:10px 0"></div>
    <div id="resultbox" style="margin:10px 0"></div>
    <details open><summary>Live log</summary><pre id="joblog">...</pre></details>
  </div>
</section>

<section id="tab-history" class="hidden">
  <div class="panel"><h2>Previous runs <button class="sec" id="refreshruns" style="float:right;padding:4px 10px">refresh</button></h2>
    <table id="runstable"><thead><tr><th>When</th><th>.csh</th><th>Status</th></tr></thead><tbody></tbody></table></div>
</section>

<section id="tab-config" class="hidden">
  <div class="panel"><h2>Configuration <span class="muted">(persisted)</span></h2>
    <div id="cfgfields"></div>
    <div style="margin-top:14px"><button id="savecfg">Save config</button> <span id="cfgmsg" class="muted"></span></div>
  </div>
</section>
</main>
<script>
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
async function jget(u){const r=await fetch(u);return r.json();}
async function jpost(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});return r.json();}
function esc(s){return(s==null?'':(''+s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
$$('.tab').forEach(t=>t.onclick=()=>{$$('.tab').forEach(x=>x.classList.remove('active'));t.classList.add('active');
  $$('main>section').forEach(s=>s.classList.add('hidden'));$('#tab-'+t.dataset.tab).classList.remove('hidden');
  if(t.dataset.tab==='history')loadRuns();if(t.dataset.tab==='config')loadConfig();});

$('#parsebtn').onclick=doPreview;
async function doPreview(){
  const p=$('#cshpath').value.trim();if(!p){$('#runmsg').textContent='enter a .csh path';return;}
  $('#runmsg').innerHTML='<span class="spinner"></span>parsing...';
  const d=await jget('/api/parse?csh='+encodeURIComponent(p));
  if(d.error){$('#runmsg').innerHTML='<span class="pill bad">'+esc(d.error)+'</span>';return;}
  $('#runmsg').textContent='';renderPreview(d);
}
function renderPreview(d){
  const pv=$('#previewpanel');pv.style.display='block';
  let h='<div class="muted" style="margin-bottom:8px">'+esc(d.path)+' &bull; '+d.commands.length+' command(s) &bull; DM: <b>'+esc(d.dm_system)+'</b> &bull; lock-check: <b>'+esc(d.lock_check)+'</b></div>';
  h+='<table><thead><tr><th>#</th><th>Command</th><th>ihdl?</th></tr></thead><tbody>';
  d.commands.forEach(c=>h+='<tr><td>'+c.n+'</td><td><code>'+esc(c.text)+'</code></td><td>'+(c.is_ihdl?'<span class="pill warn">ihdl</span>':'')+'</td></tr>');
  h+='</tbody></table>';
  if(d.ihdl_targets.length){
    h+='<h2 style="margin-top:16px">ihdl lock-check targets</h2>';
    d.ihdl_targets.forEach(t=>{
      h+='<div style="margin:6px 0"><b>cmd '+t.n+'</b>: lib=<b>'+esc(t.library||'?')+'</b> cells=<b>'+esc(t.cells.join(',')||'?')+'</b> views=<b>'+esc(t.views.join(',')||'?')+'</b>';
      if(!t.cds_lib)h+=' <span class="pill bad">no cds.lib</span>';
      h+='<table style="margin-top:4px"><thead><tr><th>cell</th><th>view</th><th>on disk</th><th>path</th></tr></thead><tbody>';
      t.cellviews.forEach(v=>h+='<tr><td>'+esc(v.cell)+'</td><td>'+esc(v.view)+'</td><td>'+(v.exists?'<span class="pill good">yes</span>':'<span class="pill muted">new</span>')+'</td><td class="muted" style="font-size:11px">'+esc(v.path||'(unresolved)')+'</td></tr>');
      h+='</tbody></table></div>';
    });
  }
  $('#previewbody').innerHTML=h;
}

let pollTimer=null,CURRENT_JOB=null,RESULT_SCROLLED=false;
$('#gobtn').onclick=async()=>{
  const p=$('#cshpath').value.trim();if(!p){$('#runmsg').textContent='enter a .csh path';return;}
  $('#gobtn').disabled=true;$('#runmsg').innerHTML='<span class="spinner"></span>launching...';
  const d=await jpost('/api/run',{csh:p});$('#gobtn').disabled=false;
  if(d.error){$('#runmsg').innerHTML='<span class="pill bad">'+esc(d.error)+'</span>';return;}
  $('#runmsg').textContent='job '+d.job_id+'  ('+d.run_dir+')';
  RESULT_SCROLLED=false;$('#livepanel').style.display='block';$('#resultbox').innerHTML='';
  $('#stopbtn').style.display='inline-block';$('#stopbtn').disabled=false;$('#stopbtn').textContent='■ Stop';
  $('#livepanel').scrollIntoView({behavior:'smooth',block:'start'});
  if(pollTimer)clearInterval(pollTimer);pollTimer=setInterval(()=>pollJob(d.job_id),1200);pollJob(d.job_id);
};
$('#stopbtn').onclick=async()=>{if(CURRENT_JOB){$('#stopbtn').disabled=true;$('#stopbtn').textContent='stopping...';await jpost('/api/stop',{job_id:CURRENT_JOB});}};
function fmtDur(s){s=Math.round(s||0);const m=Math.floor(s/60),ss=s%60;return m>0?(m+'m'+(ss<10?'0':'')+ss+'s'):(ss+'s');}
function renderProgress(d,st){
  const fill=$('#progfill'),txt=$('#progtext'),pct=$('#progpct');fill.classList.remove('indet','bad','good');
  const big='font-size:18px;font-weight:700';
  if(st==='done'){fill.classList.add('good');fill.style.width='100%';pct.innerHTML='<span style="color:var(--good)">&#10003;100%</span>';txt.innerHTML='completed in <span style="'+big+';color:var(--good)">'+fmtDur(d.elapsed)+'</span>';}
  else if(st==='failed'){fill.classList.add('bad');fill.style.width='100%';pct.innerHTML='<span style="color:var(--bad)">&#10007;</span>';txt.innerHTML='failed after <span style="'+big+';color:var(--bad)">'+fmtDur(d.elapsed)+'</span>';}
  else if(d.progress!=null){fill.style.width=d.progress+'%';pct.innerHTML='<span style="color:var(--acc)">'+d.progress+'%</span>';
    txt.innerHTML='<span class="spinner"></span>elapsed <span style="'+big+'">'+fmtDur(d.elapsed)+'</span>'+(d.eta?(' &bull; <span style="'+big+';color:var(--acc)">~'+fmtDur(d.eta)+'</span> remaining'):'')+' <span class="muted" style="font-size:12px">(est. from prior run)</span>';}
  else{fill.classList.add('indet');fill.style.width='35%';pct.innerHTML='<span class="spinner"></span>';txt.innerHTML='running&hellip; elapsed <span style="'+big+'">'+fmtDur(d.elapsed)+'</span> <span class="muted" style="font-size:12px">(no prior run for ETA)</span>';}
}
async function pollJob(jid){
  CURRENT_JOB=jid;const d=await jget('/api/job?id='+encodeURIComponent(jid));
  if(d.error){$('#jobstate').textContent=d.error;return;}
  const st=d.state,cls=st==='done'?'good':(st==='failed'?'bad':'warn');
  $('#jobstate').className='pill '+cls;$('#jobstate').textContent=st;
  $('#stopbtn').style.display=(st==='running'||st==='queued')?'inline-block':'none';
  renderProgress(d,st);
  // lock report
  if((d.lock_report||[]).length){
    let lh='<table><thead><tr><th>cell</th><th>view</th><th>status</th></tr></thead><tbody>';
    d.lock_report.forEach(r=>{const c=r.blocked?'bad':(r.status==='clean'?'good':(r.status==='self-lock'?'warn':'muted'));
      lh+='<tr><td>'+esc(r.cell)+'</td><td>'+esc(r.view)+'</td><td><span class="pill '+c+'">'+esc(r.status)+(r.owner?(' ('+esc(r.owner)+')'):'')+'</span></td></tr>';});
    lh+='</tbody></table>';$('#lockbox').innerHTML='<h2 style="margin:0 0 6px">Lock check</h2>'+lh;
  }
  $('#steps').innerHTML=(d.steps||[]).map((s,i)=>{
    const box=s.state==='done'?'<span style="color:var(--good);font-size:18px">&#9745;</span>':s.state==='failed'?'<span style="color:var(--bad);font-size:18px">&#9746;</span>':'<span style="font-size:18px;color:var(--muted)">&#9744;</span>';
    const c=s.state==='done'?'good':(s.state==='failed'?'bad':'warn');const active=s.state==='running';
    const style='display:flex;gap:10px;align-items:flex-start;margin:6px 0;padding:9px 12px;border-radius:6px;'+(active?'border:3px solid var(--acc);background:var(--acc-light)':'border:1px solid var(--line)');
    return '<div style="'+style+'">'+box+'<div style="flex:1"><b>'+(i+1)+'.</b> '+(active?'<span class="spinner"></span>':'')+'<span class="pill '+c+'">'+esc(s.state)+'</span> <b>'+esc(s.name)+'</b>'+(s.rc!=null?' <span class="muted">rc='+s.rc+'</span>':'')+'</div></div>';
  }).join('');
  $('#joblog').textContent=d.log||'';$('#joblog').scrollTop=$('#joblog').scrollHeight;
  if(d.result||d.error){
    let h='';if(d.result){const r=d.result;const ok=r.status==='OK';
      h+='<span class="pill '+(ok?'good':'bad')+'" style="font-size:14px;padding:5px 14px">'+esc(r.status)+'</span>'+
        ' <span class="muted">rc='+r.rc+' &bull; '+r.ihdl+' ihdl &bull; '+r.errors+' error(s)'+(r.schematic_created?' &bull; schematic created':'')+'</span>';}
    if(d.error)h+='<div class="pill bad" style="margin-top:8px">'+esc(d.error)+'</div>';
    $('#resultbox').innerHTML=h;}
  if((st==='done'||st==='failed')&&!RESULT_SCROLLED){RESULT_SCROLLED=true;$('#resultbox').scrollIntoView({behavior:'smooth',block:'nearest'});}
  if(st==='done'||st==='failed'){clearInterval(pollTimer);pollTimer=null;}
}
async function loadRuns(){const d=await jget('/api/runs');const tb=$('#runstable tbody');tb.innerHTML='';
  (d.runs||[]).forEach(r=>{const tr=document.createElement('tr');
    tr.innerHTML='<td>'+esc(r.run_name)+'</td><td class="muted" style="font-size:12px">'+esc(r.csh||'')+'</td><td>'+esc(r.status)+'</td>';tb.appendChild(tr);});
  if(!(d.runs||[]).length)tb.innerHTML='<tr><td colspan=3 class="muted">no runs yet</td></tr>';}
$('#refreshruns').onclick=loadRuns;
const CFG_LABELS={csh_shell:'shell for .csh (auto/tcsh/csh/sh)',cds_lib:'cds.lib path (blank=auto)',
  lock_check:'lock check on? (yes/no)',dm_system:'DM system (sos/designsync/auto)',ihdl_views:'views to lock-check',
  sos_check_cmd:'Cliosoft SOS check command ({path})',sos_locked_regex:'SOS locked regex',sos_self_ok:'allow self-locks (yes/no)',
  ds_check_cmd:'DesignSync check command ({path})',ds_locked_regex:'DesignSync locked regex',
  stop_on_lockcheck_error:'stop if lock-check errors (yes/no)',backup_logs:'back up logs before run (yes/no)',
  modules:'modules to auto-load',module_load_cmd:'module-load command ({modules})',auto_load_modules:'auto module-load (yes/no)'};
async function loadConfig(){const c=await jget('/api/config');const box=$('#cfgfields');box.innerHTML='';
  Object.keys(CFG_LABELS).forEach(k=>{const big=k.endsWith('_cmd')||k.endsWith('regex');
    const w=document.createElement('div');w.innerHTML='<label>'+esc(CFG_LABELS[k])+' <code>'+k+'</code></label>'+(big?'<textarea rows=2 id="cfg_'+k+'"></textarea>':'<input type="text" id="cfg_'+k+'">');
    box.appendChild(w);$('#cfg_'+k).value=c[k]||'';});}
$('#savecfg').onclick=async()=>{const body={};Object.keys(CFG_LABELS).forEach(k=>body[k]=$('#cfg_'+k).value);
  const d=await jpost('/api/config',body);$('#cfgmsg').textContent=d.ok?'saved':'error';setTimeout(()=>$('#cfgmsg').textContent='',2000);};
// init: prefill the --csh path
(async()=>{const s=await jget('/api/startup');if(s.csh){$('#cshpath').value=s.csh;doPreview();}})();
</script>
</body></html>
"""


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #

def _force_utf8_console():
    for nm in ("stdout", "stderr"):
        s = getattr(sys, nm)
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                import io
                setattr(sys, nm, io.TextIOWrapper(s.buffer, encoding="utf-8",
                                                  errors="replace", line_buffering=True))
            except Exception:
                pass


def main():
    global CONFIG, CONFIG_PATH, RUNS_BASE, CSH_ARG
    _force_utf8_console()
    ap = argparse.ArgumentParser(description="Run a .csh with ihdl lock-check (browser GUI)")
    ap.add_argument("--csh", default="", help="path to the .csh script to run")
    ap.add_argument("--port", type=int, default=8988)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--base", default=os.path.abspath("./csh_runs"))
    ap.add_argument("--config", default=os.path.abspath("./run_csh_config.json"))
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    CSH_ARG = os.path.abspath(os.path.expanduser(args.csh)) if args.csh else ""
    CONFIG_PATH = os.path.abspath(args.config)
    CONFIG = load_config(CONFIG_PATH)
    if not os.path.isfile(CONFIG_PATH):
        save_config(CONFIG_PATH, CONFIG)
    RUNS_BASE = os.path.abspath(args.base)
    os.makedirs(RUNS_BASE, exist_ok=True)

    httpd = None
    for port in range(args.port, args.port + 20):
        try:
            httpd = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError as e:
            if getattr(e, "errno", None) in (98, 48):
                continue
            sys.stderr.write("ERROR: bind %s:%d -> %s\n" % (args.host, port, e))
            raise SystemExit(1)
    if httpd is None:
        sys.stderr.write("ERROR: ports %d-%d busy.\n" % (args.port, args.port + 19))
        raise SystemExit(1)
    actual = httpd.server_address[1]
    url = "http://%s:%d/" % (args.host, actual)
    print("=" * 66)
    print(" Run .csh + ihdl lock-check GUI   (rev %d)" % APP_REVISION)
    print("   URL      : %s" % url)
    if actual != args.port:
        print("   (port %d busy -> using %d)" % (args.port, actual))
    print("   .csh     : %s" % (CSH_ARG or "(none -- enter in the GUI)"))
    print("   runs dir : %s" % RUNS_BASE)
    print("   config   : %s" % CONFIG_PATH)
    print("   NOTE: the .csh / soscmd / ihdl inherit THIS shell's environment.")
    print("   Ctrl-C to stop.")
    print("=" * 66)
    sys.stdout.flush()
    if args.open and os.environ.get("DISPLAY"):
        try:
            import webbrowser
            devnull = os.open(os.devnull, os.O_WRONLY); saved = os.dup(2); os.dup2(devnull, 2)
            try:
                webbrowser.open(url)
            finally:
                os.dup2(saved, 2); os.close(devnull); os.close(saved)
        except Exception:
            pass
    elif args.open:
        print("   (--open: no DISPLAY -- open the URL above yourself)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
