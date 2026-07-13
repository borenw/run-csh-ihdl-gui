# Run .csh + ihdl lock-check GUI

A dependency-free, browser-based runner for a **`.csh`** script that adds a
**design-management lock-check guard** for Cadence **`ihdl`** (Verilog-In) commands.
Before running, it verifies the target **schematic/symbol** cellviews are *not*
locked / checked out by someone else (**Cliosoft SOS** by default; DesignSync
template included). If any is blocked it **reports the error and stops**; otherwise
it backs up existing logs, runs the whole `.csh` **in the shell you launched from**
(env inherited), live-tails the log, and shows **% + ETA** from the previous run.

No Flask, no pip installs — Python 3.5+ standard library only. Single file.

## Install & run — one command

```bash
curl -fsSL -H "Accept: application/vnd.github.raw" "https://api.github.com/repos/borenw/run-csh-ihdl-gui/contents/run_csh_gui.py?ref=main" -o run_csh_gui.py && python3 run_csh_gui.py --csh /path/to/run.csh --open
```

Run it from a shell where your Cadence / Cliosoft environment is set up. Open the
printed `http://127.0.0.1:8988/`. (Uses the GitHub API endpoint so it's never stale;
the plain `raw.githubusercontent.com` URL is CDN-cached ~5 min.)

## GUI mode vs headless `--run`

By default this is a **GUI** tool: after launch it serves a page and waits — a run only
starts when you **open the printed URL in a browser and click GO**. On a headless or
remote host (no browser), launch it and nothing runs until you do that.

To run **without a browser**, add `--run` — it executes the `--csh` immediately in the
terminal (all steps, watchdog and `-E-` lines stream to stdout) and exits with the
job's status (exit code 0 = OK, 1 = failed):

```bash
python3 run_csh_gui.py --run --csh /path/to/run.csh
```

## Updating (stop → fetch → verify → relaunch)

The build number is shown **top-right in the GUI** (`rev N`) — it reflects the *running*
process, so you must **restart the server** after updating, not just re-download. This
one-liner stops the old instance, fetches the latest, prints the rev, and relaunches:

```bash
pkill -f run_csh_gui.py 2>/dev/null; \
curl -fsSL -H "Accept: application/vnd.github.raw" "https://api.github.com/repos/borenw/run-csh-ihdl-gui/contents/run_csh_gui.py?ref=main" -o run_csh_gui.py && \
echo "downloaded: $(grep -m1 APP_REVISION run_csh_gui.py)" && \
python3 run_csh_gui.py --csh /path/to/run.csh --open
```

Confirm the printed `APP_REVISION = N` (and the GUI badge) match the latest commit:
https://github.com/borenw/run-csh-ihdl-gui/commits/main

## Where is it running / stuck?  (terminal vs browser)

All progress goes to the **terminal where you launched `python3`** — *not* the browser:

- `===== STEP N =====` phase banners and `-I- entered step N` lines,
- `-I- running (…): <command>` before each subprocess, with `…still running: Ns elapsed`,
- **`-W- watchdog: in [STEP N: …], Ns elapsed`** printed every 6 s **no matter where the
  code is** — so a hang (blocked `soscmd`/SOS server, a `cds.lib`/param file on a stale
  NFS mount, an `ihdl` prompt, a license wait) is always visible instead of a silent
  stall after a banner,
- `-E-` error lines (a failed step dumps the tool's own last output).

The **browser** shows the progress bar, step checkboxes, lock-check table and live log.
If you only watch the browser you won't see the watchdog — check the terminal.

If it hangs, read the last `-W- watchdog:` line for the step, and the last `-I- running:`
line for the exact command; run that command by hand to see what it's waiting on. To
bypass the lock guard entirely, set **Config → `lock_check` = no**; tune the timeout with
`lockcheck_timeout`.

## Why a local server (not a static `.html`)

A static page can't run shell commands. This script runs a tiny HTTP server **in your
current shell**; the browser talks to `localhost`, and the `.csh` / `soscmd` / `ihdl`
calls are subprocesses that inherit `os.environ` verbatim — licenses, `PATH` and module
setup carry over with nothing to re-source.

## Flow

```
.csh ──parse──▶ list commands, flag ihdl lines
                     │  for each ihdl: parse target lib/cell/views
                     ▼      (from -param / -cdslib / .ihdlEnvFile / # LOCKCHECK)
        resolve <lib>/<cell>/<view> via cds.lib
                     ▼
   Cliosoft SOS lock-check each cellview  ──locked?──▶ STOP (report -E- BLOCKED)
                     │ clean
                     ▼
        back up existing *.log (timestamped .bak)
                     ▼
   run the whole .csh in your shell  ──live-tail log, % + ETA from prior run──▶ result
```

## How targets are found

For each `ihdl` command the tool reads the **target library / cell / views** from:
1. the command's `-param <file>` and `-cdslib <file>`,
2. a `.ihdlEnvFile` (next to the `.csh` or in `$HOME`) as fallback,
3. top `module` name(s) in the Verilog design files for the cell,
4. an explicit override directive in the `.csh` (always wins):
   ```
   # LOCKCHECK lib=myLib cell=vth_prime views=schematic,symbol
   ```
Cellview dirs `<libpath>/<cell>/<view>` are resolved via `cds.lib` (`DEFINE` /
`INCLUDE` / `SOFTINCLUDE`).

## Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--csh PATH` | — | the `.csh` to run (prefilled in the GUI; editable there) |
| `--port N` | `8988` | port (auto-hops if busy) |
| `--host H` | `127.0.0.1` | bind address |
| `--base DIR` | `./csh_runs` | run outputs + registry |
| `--config F` | `./run_csh_config.json` | config (persisted from the Config tab) |
| `--open` | off | open a browser (skipped if no `DISPLAY`) |
| `--run` | off | headless: run the `--csh` now in this terminal (no browser), then exit |

## Configuration (Config tab / env / `config.example.json`)

| Key | Meaning |
|-----|---------|
| `csh_shell` | `auto` (honour shebang) / `tcsh` / `csh` / `sh` / `bash` |
| `cds_lib` | `cds.lib` for lib→path resolution (blank = auto from `.csh` dir / parents / `$HOME`) |
| `lock_check` | master on/off for the DM lock guard |
| `dm_system` | `sos` (Cliosoft) / `designsync` / `auto` |
| `ihdl_views` | cellviews to check (default `schematic,symbol`) |
| `sos_check_cmd` | Cliosoft command per cellview; `{path}` = cellview dir, `{user}` = login |
| `sos_locked_regex` | a matching line = locked; capture group 1 = owning user (self-locks allowed if `sos_self_ok`) |
| `ds_check_cmd` / `ds_locked_regex` | same, for DesignSync |
| `stop_on_lockcheck_error` | stop if the check tool is missing / errors |
| `backup_logs` | copy existing logs to `.bak` before the run |
| `modules` / `module_load_cmd` / `auto_load_modules` | auto `module load` if the `.csh`'s tools aren't on `PATH` |

> Adjust `sos_check_cmd` / `sos_locked_regex` to match your Cliosoft client's exact
> `status` output. The default assumes `soscmd status` with a `Locked by <user>` /
> `Checked out by <user>` style line.

## Console trace

The launching terminal shows numbered `===== STEP N =====` phase banners, the exact
command per step (`------ command used for X ------`), the lock-check table, a
5-second heartbeat, `-E-` errors (a failed step dumps the tool's own output), and
`JOB DONE / FAILED`. The build number shows top-right in the GUI (`rev N`).

## Requirements

- Python 3.5+ (standard library only)
- Cadence `ihdl` + your shell (`tcsh`/`csh`); Cliosoft `soscmd` (or DesignSync) for the lock check
- A browser on the same host (or an SSH port-forward: `ssh -L 8988:127.0.0.1:8988 you@host`)

## License

MIT — see [LICENSE](LICENSE).
