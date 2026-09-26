#!/usr/bin/env python3
"""bgwatch — the Monitor command for anything long-running.

    Monitor(command="bgwatch /path/to/task.output", persistent=true, description="...")

Follows a log file and prints one line per thing worth waking up for:

  [fail]  a line matching the failure pattern (Traceback, Error, Killed, OOM, ...)
  [match] a line matching --match (your progress / success marker)
  [hb]    a heartbeat (backing off 1→10 min by default): alive?, line count, idle time, last line
          (+ the output of --probe CMD, for progress that lives outside the log)
  [STALL] silence longer than the job's own cadence (or a fixed --stall)
  [EXIT]  the job ended — then bgwatch exits, so `persistent: true` never leaks

Job end is detected without a PID: a background task's process holds its output
file open on fd 1/2, so bgwatch scans /proc/*/fd for the file. `--pid`, `--pgrep`
and `--slurm` cover jobs whose output file isn't held open by the job itself.

When there is no growing file at all (a cloud CLI that dumps and exits, a batch or
queue status you have to ask for), `--cmd CMD` makes a command the source: it is
rerun every --cmd-every seconds and the lines that changed since the previous run
are the event stream (every changed line wakes you unless --match narrows it);
`--until CMD` ends the watch when that command exits 0.

Every stdout line is a notification, so the output is deliberately sparse.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import tempfile
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from stat import S_ISREG as stat_is_reg

# Log-shaped rather than bare words: a line has to look like a failure report, not merely
# mention one. Bare case-insensitive words (error, killed, nan, failure …) fired on every
# echoed model output, test name and code listing in the 2026-09-25 archive replay; each
# alternative below keeps a failure class that replay found in real logs. Case-insensitive
# parts are scoped (?i:…) so the pattern can be OR-ed with --fail-also.
DEFAULT_FAIL = (
    r"Traceback \(most recent call last\)"            # Python, also inside rich boxes / grep output
    r"|\b[A-Z]\w*(Error|Exception)\b"                  # KeyError, TimeoutError, torch.OutOfMemoryError …
    r"|^\s*(Error|Fatal):"                              # JS / CLI "Error: …" at line start
    r"|\b(ERROR|FATAL|CRITICAL)\b"                      # upper-case log levels
    r"|\bFAILED\b(?!:?\s*(none|0)\b)|\bFailed:"          # pytest / script verdicts, not "FAILED: none"
    r"|\b(error|fatal)( [A-Z]+\d+)?:"                   # "error: …", "fatal: …", "error TS2322:"
    r"|\b[1-9]\d* (failed|errors?)\b(?!:?\s*(none|0)\b)" # non-zero failure counts, not "0 failed"
    r"|\bSample error\b"                                # inspect_ai per-sample errors
    r"|(?i:\bfailed with\b)"                            # "Runner failed with exception", "Command failed with exit code 1"
    r"|^[\w.-]+: .*\bfailed\b"                         # a program's own report: "runpod_session: setup failed on pod …"
    r"|['\"]error['\"]\s*:\s*['\"]"                     # an error payload: {'error': 'dedup call failed'}, not "error": null
    r"|\bKilled\b|SIGKILL|SIGSEGV|SIGABRT"
    r"|(?i:out of memory|\boom\b|segmentation fault|core dumped|cuda error|\btimed out\b)"
    r"|(?i:\b(loss|grad_?norm)\w*\s*[=:]\s*nan\b|\bnan (loss|detected|encountered)\b)"
    r"|(?i:command not found|no such file or directory|permission denied)"
    r"|slurmstepd|\bCANCELLED\b"
    r"|(?i:\bexit(ed)? (with )?(code|status):? [1-9])"
)
# Always-benign lines whose wording trips DEFAULT_FAIL:
# - inspect_ai's batch-status poll, "requests (pending/completed/failed): 3/40/0"
# - Modal's client, after a detached run: "Timed out waiting for final app logs."
# - glog/absl warning- and info-level lines (torch, vLLM workers), which print a
#   "Traceback (most recent call last)" as part of a warning
# - source lines inside a rich traceback box ("│ ❱ 550 │ raise RemoteError(…"): the box's
#   "Traceback" header and its final exception line already carry the failure
# - the harness's own "[exited with code N]" trailer: the completion notification and the
#   [EXIT] line already report it, and 143/144 mean the job was stopped on purpose
DEFAULT_IGNORE = (
    r"pending/completed/failed"
    r"|Timed out waiting for final app logs"
    r"|\b[IW]\d{4} \d\d:\d\d:\d\d\.\d+\s+\d+ \S+:\d+\]"
    r"|^│\s+(❱\s+)?\d+ │"
    r"|^\[exited with code \d+\]$"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
TRACEBACK_HEAD = re.compile(r"^Traceback \(most recent call last\)")
TASK_ID_RE = re.compile(r"^[a-z0-9]{6,12}$")


def resolve_target(arg: str) -> str:
    """A bare harness task id (`bgt3ng9gt`) resolves to its output file under
    <tmp>/claude-<uid>/*/*/tasks/<id>.output, so the Monitor call needs no path."""
    if TASK_ID_RE.match(arg) and not Path(arg).exists():
        uid = os.getuid() if hasattr(os, "getuid") else ""
        hits = glob.glob(os.path.join(tempfile.gettempdir(), f"claude-{uid}", "*", "*", "tasks", f"{arg}.output"))
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            hits.sort(key=os.path.getmtime)
            return hits[-1]
    return arg
LAST_LINE_MAX = 200
PROBE_MAX = 160
POLL_S = 1.0


def process_age(pid: int) -> float | None:
    """Seconds since `pid` started, from /proc/<pid>/stat field 22 and /proc/uptime."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            start_ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        return uptime - start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def fd_flags(pid: int, fd: str) -> int | None:
    try:
        with open(f"/proc/{pid}/fdinfo/{fd}") as f:
            for line in f:
                if line.startswith("flags:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    return None


O_APPEND = 0o2000
HANDOFF_S = 600  # a re-arm on the same file and job within this long continues the previous watcher
FAIL_SHOWN = 2  # an identical failure line (numbers masked) is shown this many times, then counted
HELD_FLUSH_S = 600  # counted repeats are reported at the next status line, or after this long
# files a job writes that are data, not its log (for the "the job is writing X" pointer)
NOT_A_LOG = (".eval", ".db", ".sqlite", ".sqlite3", "-journal", "-wal", ".pt", ".bin", ".safetensors",
             ".parquet", ".arrow", ".npy", ".npz", ".zip", ".tar", ".gz", ".png", ".jpg", ".mp4", ".lock")
YOUNG_JOB_S = 120


def hms(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def emit(line: str) -> None:
    print(line, flush=True)


def clip(s: str, n: int = LAST_LINE_MAX) -> str:
    s = s.rstrip("\n")
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- job liveness


class Job:
    """Answers `alive()`; `seen` flips once the job has been observed at least once,
    so 'never found' (job-end detection off) and 'found, then gone' (EXIT) differ."""

    seen = False
    kind = "none"

    def alive(self) -> bool | None:  # None = unknown
        return None

    def describe(self) -> str:
        return self.kind

    def key(self) -> list:
        return []


class FdHolderJob(Job):
    """Alive while some process other than us holds the file open."""

    kind = "fd-holder"

    def __init__(self, path: Path):
        self.target = str(path.resolve())
        self.me = os.getpid()
        self.pids: set[int] = set()
        self.appends = False  # some holder opened the file with O_APPEND (`>>`, or the harness)

    def alive(self) -> bool:
        pids = set()
        for fd_dir in Path("/proc").glob("[0-9]*/fd"):
            pid = int(fd_dir.parent.name)
            if pid == self.me:
                continue
            try:
                for fd in fd_dir.iterdir():
                    try:
                        if os.readlink(fd) == self.target:
                            pids.add(pid)
                            flags = fd_flags(pid, fd.name)
                            if flags is not None and flags & O_APPEND:
                                self.appends = True
                            break
                    except OSError:
                        continue
            except OSError:  # process vanished or not ours
                continue
        if pids:
            self.seen = True
            self.pids = pids
        return bool(pids)

    def describe(self) -> str:
        return f"pid {min(self.pids)}" if self.pids else "no process holds the file"

    def key(self) -> list:
        return sorted(self.pids)

    def age(self) -> float | None:
        ages = [a for a in (process_age(p) for p in self.pids) if a is not None]
        return max(ages) if ages else None

    def other_writes(self, limit: int = 2) -> list[str]:
        """Regular files the job's process tree has open for writing, other than the watched
        one: where the output went when the watched file stays empty (a task-id watch on a
        command that redirects to its own log)."""
        children: dict[int, list[int]] = {}
        for stat in Path("/proc").glob("[0-9]*/stat"):
            try:
                ppid = int(stat.read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            children.setdefault(ppid, []).append(int(stat.parent.name))
        todo, tree = list(self.pids), set()
        while todo:
            pid = todo.pop()
            if pid not in tree:
                tree.add(pid)
                todo.extend(children.get(pid, []))
        found: dict[str, float] = {}
        for pid in tree:
            try:
                fds = list(Path(f"/proc/{pid}/fd").iterdir())
            except OSError:
                continue
            for fd in fds:
                try:
                    path = os.readlink(fd)
                except OSError:
                    continue
                if (not path.startswith("/") or path == self.target or path.startswith(("/dev/", "/proc/"))
                        or path.endswith(NOT_A_LOG)):
                    continue
                flags = fd_flags(pid, fd.name)
                if flags is None or not flags & 0o3:  # O_WRONLY | O_RDWR
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if stat_is_reg(st.st_mode):
                    found[path] = st.st_mtime
        return sorted(found, key=found.get, reverse=True)[:limit]


class PidJob(Job):
    kind = "pid"

    def __init__(self, pid: int):
        self.pid = pid

    def alive(self) -> bool:
        try:
            with open(f"/proc/{self.pid}/stat") as f:
                state = f.read().rsplit(")", 1)[1].split()[0]
        except OSError:
            return False
        self.seen = True
        return state != "Z"

    def describe(self) -> str:
        return f"pid {self.pid}"

    def key(self) -> list:
        return [self.pid]


def ancestors(pid: int) -> set[int]:
    out = set()
    while pid > 1:
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        out.add(pid)
    return out


class PgrepJob(Job):
    """`pgrep -f` minus bgwatch itself and every ancestor: the harness runs a Monitor
    command through `bash -c "source … && bgwatch … --pgrep PAT"`, whose own command line
    contains PAT (found by the first whowill A/B — 3/8 sessions got a false 'alive')."""

    kind = "pgrep"

    def __init__(self, pattern: str):
        self.pattern = pattern
        self.exclude = {os.getpid()} | ancestors(os.getpid())

    def alive(self) -> bool:
        out = subprocess.run(["pgrep", "-f", self.pattern], capture_output=True, text=True).stdout
        pids = {int(p) for p in out.split() if p.strip()} - self.exclude
        if pids:
            self.seen = True
        return bool(pids)

    def describe(self) -> str:
        return f"pgrep -f {self.pattern!r}"


class SlurmJob(Job):
    """squeue while queued/running, sacct for the final state. Untested on the dev
    box (no slurm here); the command shapes are the standard ones."""

    kind = "slurm"

    def __init__(self, jobid: str):
        self.jobid = jobid
        self.state = "?"

    def alive(self) -> bool:
        q = subprocess.run(["squeue", "-h", "-j", self.jobid, "-o", "%T"], capture_output=True, text=True)
        st = q.stdout.strip().split("\n")[0].strip() if q.stdout.strip() else ""
        if st:
            self.seen = True
            self.state = st
            return True
        a = subprocess.run(["sacct", "-n", "-X", "-j", self.jobid, "-o", "State"], capture_output=True, text=True)
        self.state = (a.stdout.strip().split("\n")[0].strip() or "UNKNOWN").split()[0]
        return False

    def describe(self) -> str:
        return f"slurm {self.jobid} {self.state}"

    def key(self) -> list:
        return [self.jobid]


# ------------------------------------------------------------------- handoff


def state_path_for(path: Path) -> Path:
    """Where a watcher leaves its position for the next one on the same file."""
    uid = os.getuid() if hasattr(os, "getuid") else ""
    key = hashlib.sha1(str(path.expanduser().resolve()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"bgwatch-{uid}" / f"{key}.json"


def fail_key(text: str) -> str:
    """A failure line with every token that holds a digit masked: 'Sample error (id: p1, epoch: 1)'
    and '(id: hr8, epoch: 1)' are the same failure."""
    return re.sub(r"\S*\d\S*", "#", " ".join(text.split()))[:300]


def ctl_path_for(path: Path) -> Path:
    return state_path_for(path).with_suffix(".ctl.json")


def changed_lines(prev: list[str], cur: list[str]) -> list[str]:
    """Lines of `cur` not accounted for by `prev`, counted as a multiset, in order: a rolling log
    window yields its new tail, a status dump the lines that changed. A line identical to one
    still in the previous output is not new, even if the program printed it again."""
    left = Counter(prev)
    out = []
    for line in cur:
        if left[line] > 0:
            left[line] -= 1
        else:
            out.append(line)
    return out


def start_cmd_poller(a: argparse.Namespace) -> Path:
    """--cmd: a child process reruns the command and writes what changed to a file it holds open,
    so everything built for files (fd-holder job end, stall, heartbeats, ctl) applies unchanged.
    The child exits when --until holds or when this watcher goes away."""
    uid = os.getuid() if hasattr(os, "getuid") else ""
    key = hashlib.sha1(a.cmd.encode()).hexdigest()[:12]
    path = Path(tempfile.gettempdir()) / f"bgwatch-{uid}" / f"cmd-{key}-{os.getpid()}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, os.path.abspath(__file__), "_cmdpoll", "--parent", str(os.getpid()),
            "--every", str(a.cmd_every), "--cmd", a.cmd]
    if a.until:
        args += ["--until", a.until]
    if a.from_start:
        args.append("--baseline")
    with open(path, "w") as out:
        subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    return path


def cmd_poller_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="bgwatch _cmdpoll")
    p.add_argument("--cmd", required=True)
    p.add_argument("--until")
    p.add_argument("--every", type=float, required=True)
    p.add_argument("--parent", type=int, required=True)
    p.add_argument("--baseline", action="store_true")
    a = p.parse_args(argv)
    timeout = max(60.0, 2 * a.every)

    def run(cmd: str) -> tuple[int | None, list[str]]:
        try:
            r = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, text=True, errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, []
        return r.returncode, r.stdout.splitlines()

    prev: list[str] | None = None
    while os.getppid() == a.parent:
        rc, lines = run(a.cmd)
        if rc is None:
            print(f"[bgwatch --cmd] the command timed out after {timeout:.0f}s", flush=True)
        elif rc != 0:
            # prev is kept, so the next good run is diffed against the last good one, not the error text
            print(f"[bgwatch --cmd] the command exited with code {rc}: {lines[-1] if lines else '(no output)'}", flush=True)
        else:
            if prev is None and not a.baseline:
                print(f"[bgwatch --cmd] baseline: {len(lines)} lines; printing what changes", flush=True)
            else:
                for line in lines if prev is None else changed_lines(prev, lines):
                    print(line, flush=True)
            prev = lines
        if a.until and run(a.until)[0] == 0:
            print(f"[bgwatch --cmd] --until holds: {a.until}", flush=True)
            return 0
        deadline = time.monotonic() + a.every
        while time.monotonic() < deadline and os.getppid() == a.parent:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return 0


def pid_running(pid: int) -> bool:
    """Linux (/proc); elsewhere a still-running watcher reads as gone. A zombie (killed, not yet
    reaped by the harness) counts as gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


# ------------------------------------------------------------------- watcher


class Watcher:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.path = Path(resolve_target(a.file)).expanduser() if a.file else None
        self.fail_re = re.compile(a.fail) if a.fail else None
        self.match_re = re.compile(a.match) if a.match else None
        self.ignore_re = re.compile(a.ignore) if a.ignore else None
        self.job = self._make_job()
        self.t0 = time.monotonic()
        self.lines = 0
        self.last_line = ""
        self.last_write = time.monotonic()
        self.fail_count = 0
        self.window_start = time.monotonic()
        self.window_emitted = 0
        self.suppressed = 0
        self.stalled = False
        self.tb_buffer: list[str] | None = None
        self.tail: list[str] = []
        self.max_gap = 0.0  # longest silence between two lines seen so far
        self.hb_interval = a.every_min if a.every == "auto" else float(a.every)
        self.bytes_before = 0  # file content already there when we attached at its end
        self.state_file = state_path_for(self.path) if self.path else None
        self._last_save = 0.0
        self.ctl_file = ctl_path_for(self.path) if self.path else None
        self.ctl_seq = 0
        self._ctl_mtime = None
        self.fail_src, self.ignore_src, self.match_src = a.fail, a.ignore, a.match
        self.fail_seen: dict[str, int] = {}
        self.fail_held: dict[str, list] = {}  # key -> [count since last report, last text]
        self._held_since = time.monotonic()
        self.pointed_elsewhere = False

    def _make_job(self) -> Job:
        """--pid / --slurm are authoritative. With a file, the fd-holder scan comes first and
        --pgrep is only the fallback if nothing holds the file after --grace (10/32 A/B
        sessions passed --pgrep although the scan would have found the pid)."""
        a = self.a
        if a.pid:
            return PidJob(a.pid)
        if a.slurm:
            return SlurmJob(a.slurm)
        if self.path:
            return FdHolderJob(self.path)
        if a.pgrep:
            return PgrepJob(a.pgrep)
        return Job()

    # ---- line handling
    def _remember(self, line: str) -> None:
        now = time.monotonic()
        self.silence_start = self.last_write
        # The silence before the first line counts too: a block-buffered job that prints nothing
        # for 40 min and then 88 lines at once has a 40-min cadence, not a 0-s one.
        self.max_gap = max(self.max_gap, now - self.last_write)
        self.lines += 1
        self.last_write = now
        if line.strip():
            self.last_line = line
            self.tail.append(line)
            if len(self.tail) > self.a.tail:
                self.tail.pop(0)
        if self.stalled:
            self.stalled = False
            emit(f"[resumed {hms(self.elapsed)}] new output after {hms(now - self.silence_start)} of silence "
                 f"(the job is still running) · {clip(line)}")

    def _rate_ok(self) -> bool:
        now = time.monotonic()
        if now - self.window_start > 60:
            if self.suppressed:
                emit(f"[bgwatch] …{self.suppressed} matching lines suppressed in the last minute (--max-rate {self.a.max_rate})")
            self.window_start, self.window_emitted, self.suppressed = now, 0, 0
        if self.window_emitted >= self.a.max_rate:
            self.suppressed += 1
            return False
        self.window_emitted += 1
        return True

    def _emit_match(self, tag: str, text: str) -> None:
        if tag == "fail":
            k = fail_key(text)
            self.fail_seen[k] = self.fail_seen.get(k, 0) + 1
            if self.fail_seen[k] > FAIL_SHOWN:
                held = self.fail_held.setdefault(k, [0, text])
                held[0] += 1
                held[1] = text
                return
        if self._rate_ok():
            emit(f"[{tag}] {clip(text)}")

    def flush_held(self) -> None:
        """Report failures that repeated after being shown FAIL_SHOWN times: one line each."""
        for count, text in self.fail_held.values():
            emit(f"[fail ×{count}] {clip(text)} (the same failure again, numbers aside; last one shown)")
        self.fail_held.clear()
        self._held_since = time.monotonic()

    def handle(self, line: str) -> None:
        line = ANSI_RE.sub("", line)
        self._remember(line)
        if self.tb_buffer is not None:
            # inside a traceback: frames are indented; first flush-left line is the exception
            if line.startswith((" ", "\t")) or not line.strip():
                self.tb_buffer.append(line)
                if len(self.tb_buffer) < 60:
                    return
            self.fail_count += 1
            self._emit_match("fail", f"Traceback → {line.strip()}")
            self.tb_buffer = None
            return
        if self.ignore_re and self.ignore_re.search(line):
            return
        if self.fail_re and self.fail_re.search(line):
            if TRACEBACK_HEAD.match(line):
                self.tb_buffer = []
                return
            self.fail_count += 1
            self._emit_match("fail", line)
            return
        if self.match_re and self.match_re.search(line):
            self._emit_match("match", line)

    # ---- adaptive defaults
    @property
    def stall_threshold(self) -> float:
        """Fixed --stall S (0 = off), or auto: silence is a stall when it is 5x the longest
        gap the job itself has shown, clamped to [--stall-min, --stall-max]. Before the job
        has printed twice its cadence is unknown, so only --stall-max applies."""
        a = self.a
        if a.stall != "auto":
            return float(a.stall)
        if self.lines < 2:
            return a.stall_max
        return min(max(5 * self.max_gap, a.stall_min), a.stall_max)

    def next_hb_interval(self) -> float:
        """Fixed --every S, or auto: --every-min doubling each heartbeat up to --every-max,
        so a 4-minute job gets a heartbeat and a 4-hour job gets one every 10 minutes."""
        cur = self.hb_interval
        if cur <= 0:
            return float("inf")  # --every 0: heartbeats off
        if self.a.every == "auto":
            self.hb_interval = min(cur * 2, self.a.every_max)
        return cur

    def describe_defaults(self) -> str:
        a = self.a
        if a.every == "auto":
            hb = f"{hms(a.every_min)}→{hms(a.every_max)} backoff"
        else:
            hb = f"every {hms(float(a.every))}" if float(a.every) > 0 else "off"
        if a.stall == "auto":
            st = f"auto (5x the job's own cadence, {hms(a.stall_min)}–{hms(a.stall_max)})"
        else:
            st = f"after {hms(float(a.stall))}" if float(a.stall) else "off"
        probe = f" · probe {a.probe!r} (timeout {a.probe_timeout:g}s)" if a.probe else ""
        return f"heartbeat {hb} · stall {st}{probe}"

    # ---- status lines
    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def probe(self) -> str | None:
        """--probe CMD, run on every status line: for a job whose real progress signal is not
        in its log (an API-side batch, a queue depth), so each wake carries a number instead
        of `0 lines`. Blocks the poll loop for up to --probe-timeout."""
        if not self.a.probe:
            return None
        try:
            p = subprocess.run(
                self.a.probe, shell=True, capture_output=True, text=True, timeout=self.a.probe_timeout
            )
        except subprocess.TimeoutExpired:
            return f"probe timed out after {self.a.probe_timeout:g}s"
        except Exception as e:  # a probe must never take the watcher down with it
            return f"probe failed: {e}"
        out = " · ".join(l.strip() for l in (p.stdout + p.stderr).splitlines() if l.strip())
        if p.returncode != 0:
            return clip(f"probe rc={p.returncode}" + (f": {out}" if out else ""), PROBE_MAX)
        return clip(out, PROBE_MAX) or None

    def status(self, tag: str, alive: bool | None, note: str = "") -> str:
        state = {True: "alive", False: "ended", None: "job state unknown"}[alive]
        parts = [note] if note else []
        parts.append(state)
        if self.path:
            if self.bytes_before:
                parts.append(f"{self.lines} new lines (file had {self.bytes_before / 1000:.1f} kB before bgwatch attached)")
            else:
                parts.append(f"{self.lines} lines")
            if self.lines and tag != "STALL":
                parts.append(f"idle {hms(time.monotonic() - self.last_write)}")
            if not self.lines and alive and isinstance(self.job, FdHolderJob) and not self.pointed_elsewhere:
                elsewhere = self.job.other_writes()
                if elsewhere:
                    self.pointed_elsewhere = True  # say it once
                    parts.append(f"nothing written here yet; the job has {', '.join(elsewhere)} open for writing — if that is its log, re-arm bgwatch on it")
        if self.fail_count:
            parts.append(f"{self.fail_count} fail line{'s' if self.fail_count > 1 else ''} so far")
        probe = self.probe()
        if probe:
            parts.append(probe)
        if self.last_line:
            parts.append(f"last: {clip(self.last_line)}")
        return f"[{tag} {hms(self.elapsed)}] " + " · ".join(parts)

    # ---- main loop
    def run(self) -> int:
        a = self.a
        f = None
        if self.path:
            f = self._open_wait()
            if f is None:
                return 2
        alive = self.job.alive() if self.job.kind != "none" else None
        if self.job.kind == "fd-holder" and not alive:
            # give the job a moment to start (the Monitor is usually armed right after the launch)
            deadline = time.monotonic() + a.grace
            while time.monotonic() < deadline and not alive:
                time.sleep(0.5)
                alive = self.job.alive()
        fallback = ""
        if self.job.kind == "fd-holder" and not self.job.seen and a.pgrep:
            self.job = PgrepJob(a.pgrep)
            alive = self.job.alive()
            fallback = " (fallback: no process held the file)"
        if a.once:
            if f:  # a status line should say how much the job has written so far
                for line in f:
                    self._remember(line.rstrip("\n"))
            emit(self.status("status", alive if self.job.seen else None))
            return 0
        if self.job.kind in ("fd-holder", "pgrep") and not self.job.seen and not a.no_exit:
            # A watcher that can never see its job end is the leaked persistent Monitor
            # this tool exists to prevent, so give up loudly instead of running until TaskStop.
            emit(
                f"[EXIT {hms(self.elapsed)}] nothing to watch: no process held {self.path or ''} "
                f"{'and nothing matched --pgrep ' + repr(a.pgrep) + ' ' if a.pgrep else ''}within {a.grace:g}s. "
                "If the job already finished, its completion notification has the result; otherwise re-arm "
                "with --pid/--pgrep/--slurm, or --no-exit to follow the file regardless."
            )
            return 4
        detect = "off (--no-exit: following the file until TaskStop)" if not self.job.seen else self.job.describe() + fallback
        handoff = self.take_handoff(f) if f else None
        resumed = ""
        if handoff:
            unread = max(0, os.fstat(f.fileno()).st_size - handoff["offset"])
            resumed = (f" · continuing the previous watcher of this job (stopped {hms(time.time() - handoff['updated'])} ago): "
                       f"{handoff['lines']} lines already seen, {unread / 1000:.1f} kB written since, read now")
        emit(
            f"[bgwatch] watching {self.path or self.job.describe()} · job-end detection: {detect}"
            f" · {self.describe_defaults()}{resumed}"
        )
        if handoff:
            self._resume(f, handoff)
        elif f and not (a.from_start or self._fresh_output()):
            f.seek(0, os.SEEK_END)
            self.bytes_before = f.tell()
        self.save_state(f, force=True)
        next_hb = time.monotonic() + self.next_hb_interval()
        next_job_check = time.monotonic() + a.check_every
        job_alive = alive
        while True:
            progressed = False
            if f:
                progressed = self._drain(f)
                if progressed:
                    self.save_state(f)
                if not self.path.exists():
                    emit(f"[EXIT {hms(self.elapsed)}] {self.path} disappeared — stopping")
                    return 3
            now = time.monotonic()
            if now >= next_job_check and self.job.kind != "none":
                job_alive = self.job.alive()
                next_job_check = now + a.check_every
                if self.job.seen and not job_alive:
                    if f:
                        self._drain(f)  # last lines written before exit
                    self._emit_exit()
                    if not a.no_exit:
                        return 0
                    self.job = Job()  # keep watching the file, stop asking
            if self.poll_ctl():
                next_hb = now + self.next_hb_interval()
                self.save_state(f, force=True)
            if self.fail_held and now - self._held_since >= HELD_FLUSH_S:
                self.flush_held()
            thr = self.stall_threshold
            if thr and not self.stalled and now - self.last_write >= thr and (job_alive or job_alive is None):
                self.stalled = True
                self.flush_held()
                emit(self.status("STALL", job_alive, note=f"no output for {hms(now - self.last_write)}"))
            if now >= next_hb:
                self.flush_held()
                emit(self.status("hb", job_alive if self.job.seen else None))
                next_hb = now + self.next_hb_interval()
                self.save_state(f, force=True)
            if not progressed:
                time.sleep(a.poll)

    def poll_ctl(self) -> bool:
        """Apply a `bgwatch ctl` request if one arrived. True when the heartbeat schedule changed."""
        if not self.ctl_file:
            return False
        try:
            mtime = self.ctl_file.stat().st_mtime_ns
        except OSError:
            return False
        if mtime == self._ctl_mtime:
            return False
        self._ctl_mtime = mtime
        try:
            req = json.loads(self.ctl_file.read_text())
        except (OSError, ValueError):
            return False
        if req.get("seq", 0) <= self.ctl_seq:
            return False
        self.ctl_seq = req["seq"]
        return self.apply_ctl(req.get("changes") or {})

    def apply_ctl(self, ch: dict) -> bool:
        a, said, resched = self.a, [], False
        if "fail" in ch:
            self.fail_src = ch["fail"] or None
            said.append(f"--fail {ch['fail']!r}" if ch["fail"] else "failure patterns off")
        if ch.get("fail_also"):
            self.fail_src = f"(?:{self.fail_src})|(?:{ch['fail_also']})" if self.fail_src else ch["fail_also"]
            said.append(f"--fail-also {ch['fail_also']!r}")
        if ch.get("ignore"):
            self.ignore_src = f"(?:{self.ignore_src})|(?:{ch['ignore']})" if self.ignore_src else ch["ignore"]
            said.append(f"now also ignoring {ch['ignore']!r}")
        if "match" in ch:
            self.match_src = ch["match"] or None
            said.append(f"--match {ch['match']!r}" if ch["match"] else "--match off")
        self.fail_re = re.compile(self.fail_src) if self.fail_src else None
        self.ignore_re = re.compile(self.ignore_src) if self.ignore_src else None
        self.match_re = re.compile(self.match_src) if self.match_src else None
        if self.ignore_re:  # held repeats of what is now declared noise are dropped, not reported later
            self.fail_held = {k: v for k, v in self.fail_held.items() if not self.ignore_re.search(v[1])}
        if "every" in ch:
            a.every = ch["every"]
            self.hb_interval = a.every_min if a.every == "auto" else float(a.every)
            resched = True
        if "every_max" in ch:
            a.every_max = float(ch["every_max"])
            self.hb_interval = min(self.hb_interval, a.every_max) if self.hb_interval > 0 else self.hb_interval
            resched = True
        if "stall" in ch:
            a.stall = ch["stall"]
            self.stalled = False
        if "probe" in ch:
            a.probe = ch["probe"] or None
            said.append(f"--probe {ch['probe']!r}" if ch["probe"] else "probe off")
        if resched or "stall" in ch:
            said.append(self.describe_defaults().split(" · probe")[0])
        emit(f"[ctl {hms(self.elapsed)}] now: " + " · ".join(said) + " (read position, counts and backoff kept)")
        return resched

    def save_state(self, f, force: bool = False) -> None:
        """Leave this watcher's position and cadence for a re-arm on the same file (a restart to
        change flags). Throttled; a SIGKILLed watcher loses at most the last couple of seconds."""
        now = time.monotonic()
        if not self.state_file or f is None or (not force and now - self._last_save < 2):
            return
        self._last_save = now
        try:
            d = {"pid": os.getpid(), "updated": time.time(), "inode": os.fstat(f.fileno()).st_ino, "offset": f.tell(),
                 "job": self.job.key(), "lines": self.lines, "fail_count": self.fail_count, "max_gap": self.max_gap,
                 "t0_wall": time.time() - self.elapsed, "last_write_wall": time.time() - (now - self.last_write),
                 "hb_interval": self.hb_interval, "last_line": self.last_line, "tail": self.tail,
                 "ctl_seq": self.ctl_seq, "argv": sys.argv[1:]}
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, self.state_file)
        except (OSError, ValueError):
            pass  # a lost handoff only means the next watcher starts fresh

    def take_handoff(self, f) -> dict | None:
        """The previous watcher's state, if it watched this same file and this same job, stopped
        less than HANDOFF_S ago, and isn't still running. A relaunched job (new pids), a replaced
        or truncated file, or --fresh all start fresh."""
        if self.a.fresh or not self.state_file:
            return None
        try:
            d = json.loads(self.state_file.read_text())
            size, inode = os.fstat(f.fileno()).st_size, os.fstat(f.fileno()).st_ino
        except (OSError, ValueError):
            return None
        if not 0 <= time.time() - d.get("updated", 0) <= HANDOFF_S:
            return None
        if d.get("pid") != os.getpid() and pid_running(d.get("pid", 0)):
            return None
        if d.get("inode") != inode or d.get("offset", 0) > size:
            return None
        mine, theirs = set(map(str, self.job.key())), set(map(str, d.get("job") or []))
        if not mine or not (mine & theirs):
            return None
        return d

    def _resume(self, f, d: dict) -> None:
        now, wall = time.monotonic(), time.time()
        f.seek(d["offset"])
        self.lines, self.fail_count, self.max_gap = d["lines"], d["fail_count"], d["max_gap"]
        self.t0 = now - (wall - d["t0_wall"])
        self.last_write = now - (wall - d["last_write_wall"])
        self.hb_interval = d["hb_interval"] if self.a.every == "auto" else self.hb_interval
        self.last_line, self.tail = d.get("last_line", ""), d.get("tail", [])

    def _fresh_output(self) -> bool:
        """The job started moments ago and the file holds only its output (a harness task file,
        or a `>` redirect rather than `>>`): read it from the top, since instances arm the watcher
        a few seconds after the launch and an early failure would otherwise be skipped."""
        job = self.job
        if not (isinstance(job, FdHolderJob) and job.seen):
            return False
        age = job.age()
        if age is None or age > YOUNG_JOB_S:
            return False
        task_file = self.path.parent.name == "tasks" and self.path.suffix == ".output"
        return task_file or not job.appends

    def _open_wait(self):
        deadline = time.monotonic() + self.a.grace
        while True:
            try:
                return open(self.path, "r", errors="replace")
            except FileNotFoundError:
                if time.monotonic() > deadline:
                    emit(f"[bgwatch] {self.path} does not exist after {self.a.grace}s — giving up")
                    return None
                time.sleep(0.5)

    def _drain(self, f) -> bool:
        got = False
        while True:
            line = f.readline()
            if not line:
                return got
            if not line.endswith("\n"):
                # partial line: rewind so we re-read it once complete
                f.seek(f.tell() - len(line.encode("utf-8", errors="replace")))
                return got
            got = True
            self.handle(line.rstrip("\n"))

    def _emit_exit(self) -> None:
        self.flush_held()
        head = f"[EXIT {hms(self.elapsed)}] job ended ({self.job.describe()})"
        parts = [head]
        if self.path:
            parts.append(f"{self.lines} lines")
        if self.fail_count:
            parts.append(f"{self.fail_count} fail line{'s' if self.fail_count > 1 else ''} seen")
        emit(" · ".join(parts))
        for line in self.tail:  # printed within 200 ms → batched into the same notification
            emit(f"    {clip(line)}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bgwatch",
        description=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "patterns: --fail REPLACES the default failure regex, --fail-also EXTENDS it, --ignore\n"
            "drops lines (checked first) and EXTENDS a small default ignore list.\n"
            "`bgwatch --print-defaults` shows both defaults.\n"
            "examples:\n"
            "  bgwatch bgt3ng9gt                                    # harness background task, by id\n"
            "  bgwatch train.log --match 'step \\d+00 ' --every 900   # progress every 100 steps, fixed 15-min heartbeat\n"
            "  bgwatch train.log --ignore 'error_rate=' --fail-also 'loss=nan|diverged'\n"
            "  bgwatch batch.log --probe 'python batch_status.py'   # each wake carries the API-side count\n"
            "  bgwatch --cmd 'modal app logs my-app' --match 'Started|Loaded' --cmd-every 45   # a dump-and-exit log CLI\n"
            "  bgwatch --cmd 'python batch_status.py' --until 'python batch_status.py | grep -q ended'   # no file at all\n"
            "  bgwatch slurm-44297.out --slurm 44297\n"
            "  bgwatch server.log --pgrep 'vllm serve' --fail 'CUDA|Killed'\n"
            "  bgwatch --once train.log                             # one status line, then exit\n"
            "  bgwatch ctl train.log --ignore 'Sample error'        # change a running watcher in place (see bgwatch ctl -h)"
        ),
    )
    p.add_argument("file", nargs="?", help="log / task output file to follow, or a bare harness task id (resolved under the tmp task dirs)")
    p.add_argument("--every", default="auto", metavar="S|auto", help="heartbeat interval in seconds (0 = off); auto (default) backs off from --every-min to --every-max")
    p.add_argument("--every-min", type=float, default=60, metavar="S", help="first heartbeat interval in auto mode (default 60)")
    p.add_argument("--every-max", type=float, default=600, metavar="S", help="heartbeat interval cap in auto mode (default 600)")
    p.add_argument("--stall", default="auto", metavar="S|auto", help="warn when nothing was written for S seconds (0 = off); auto (default) = 5x the job's longest gap so far, clamped to [--stall-min, --stall-max]")
    p.add_argument("--stall-min", type=float, default=180, metavar="S", help="auto stall floor (default 180)")
    p.add_argument("--stall-max", type=float, default=1800, metavar="S", help="auto stall cap, also the threshold before the job has printed twice (default 1800)")
    p.add_argument("--match", metavar="RE", help="also emit lines matching this regex (progress / success markers)")
    p.add_argument("--fail", metavar="RE", default=DEFAULT_FAIL, help="failure regex; replaces the default")
    p.add_argument("--fail-also", metavar="RE", help="extend the failure regex with this alternative")
    p.add_argument("--no-fail", action="store_true", help="disable the failure regex entirely (only --match, heartbeats, exit)")
    p.add_argument("--ignore", metavar="RE", help="never emit lines matching this (checked before --fail/--match); extends the default ignore list")
    p.add_argument("--no-default-ignore", action="store_true", help="drop the built-in ignore list (keeps --ignore)")
    p.add_argument("--probe", metavar="CMD", help="shell command run on every status line; its output is appended to the line (for progress that lives outside the log — an API-side batch, a queue depth)")
    p.add_argument("--probe-timeout", type=float, default=45, metavar="S", help="kill a --probe that takes longer than this (default 45)")
    p.add_argument("--cmd", metavar="CMD", help="no file: rerun CMD every --cmd-every seconds and treat the lines that changed since the previous run as the log (every changed line is a [match] unless --match is given). The first run is the baseline, not printed unless --from-start. A failing run is a [fail] line")
    p.add_argument("--cmd-every", type=float, default=60, metavar="S", help="--cmd rerun interval (default 60)")
    p.add_argument("--until", metavar="CMD", help="with --cmd: end the watch ([EXIT]) once CMD exits 0, checked after each run")
    p.add_argument("--pid", type=int, help="the job is this pid")
    p.add_argument("--pgrep", metavar="PATTERN", help="the job is `pgrep -f PATTERN` — with a file, only used if nothing holds the file after --grace")
    p.add_argument("--slurm", metavar="JOBID", help="the job is this slurm job (squeue/sacct)")
    p.add_argument("--from-start", action="store_true", help="scan the existing content too (default: from the top when the job started under 2 min ago and the file holds only its output, else from the end)")
    p.add_argument("--tail", type=int, default=5, metavar="N", help="lines of tail in the EXIT summary (default 5)")
    p.add_argument("--max-rate", type=int, default=20, metavar="N", help="max [fail]/[match] lines per minute (default 20)")
    p.add_argument("--grace", type=float, default=15, metavar="S", help="seconds to wait for the file / the job to appear (default 15)")
    p.add_argument("--check-every", type=float, default=5, metavar="S", help="job liveness poll interval (default 5)")
    p.add_argument("--poll", type=float, default=POLL_S, metavar="S", help="file poll interval (default 1); also the granularity of the auto stall cadence estimate")
    p.add_argument("--no-exit", action="store_true", help="keep following the file after the job ends")
    p.add_argument("--fresh", action="store_true", help="don't continue a previous watcher of this file and job (default: a re-arm within 10 min picks up its read position, cadence and heartbeat backoff)")
    p.add_argument("--once", action="store_true", help="print one status line and exit (a poll that isn't a cat)")
    p.add_argument("--print-defaults", action="store_true", help="print the default failure / ignore regexes and exit")
    return p


def watched_path(target: str) -> Path | None:
    """The file a target names: a watched file, a job's task id, or a Monitor's own task id (its
    output starts with bgwatch's `[bgwatch] watching <file>` banner)."""
    path = Path(resolve_target(target)).expanduser()
    if state_path_for(path).exists():
        return path
    try:
        with open(path, errors="replace") as f:
            head = f.readline()
    except OSError:
        return None
    m = re.match(r"\[bgwatch\] watching (\S+)", head)
    return Path(m.group(1)) if m else None


def ctl_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="bgwatch ctl",
        description="Change the settings of running bgwatch watchers in place: read position, counts, "
                    "cadence estimate and heartbeat backoff are kept. The watcher confirms with one [ctl] line.")
    p.add_argument("targets", nargs="+", metavar="TARGET", help="watched file, the job's task id, or the Monitor's task id (several allowed)")
    p.add_argument("--fail", metavar="RE", help="replace the failure regex ('' turns failure patterns off)")
    p.add_argument("--fail-also", metavar="RE", help="extend the failure regex")
    p.add_argument("--ignore", metavar="RE", help="also ignore lines matching RE (adds to what is ignored already)")
    p.add_argument("--match", metavar="RE", help="replace the --match regex ('' turns it off)")
    p.add_argument("--every", metavar="S|auto", help="heartbeat interval (0 = off)")
    p.add_argument("--every-max", type=float, metavar="S", help="heartbeat backoff cap")
    p.add_argument("--stall", metavar="S|auto", help="stall threshold (0 = off)")
    p.add_argument("--probe", metavar="CMD", help="replace the probe command ('' turns it off)")
    p.add_argument("--wait", type=float, default=5, metavar="S", help="seconds to wait for the watcher to confirm (default 5)")
    a = p.parse_args(argv)
    changes = {}
    for name in ("fail", "fail_also", "ignore", "match"):
        v = getattr(a, name)
        if v is not None:
            if v:
                try:
                    re.compile(v)
                except re.error as e:
                    p.error(f"--{name.replace('_', '-')}: bad regex: {e}")
            changes[name] = v
    for name in ("every", "stall"):
        v = getattr(a, name)
        if v is not None:
            if v != "auto":
                try:
                    float(v)
                except ValueError:
                    p.error(f"--{name}: expected seconds or 'auto', got {v!r}")
            changes[name] = v
    if a.every_max is not None:
        changes["every_max"] = a.every_max
    if a.probe is not None:
        changes["probe"] = a.probe
    if not changes:
        p.error("nothing to change")
    rc = 0
    for t in a.targets:
        path = watched_path(t)
        state = None
        if path:
            try:
                state = json.loads(state_path_for(path).read_text())
            except (OSError, ValueError):
                state = None
        if not state or not pid_running(state.get("pid", 0)):
            print(f"bgwatch ctl: {t}: no running bgwatch watches it (arm one with Monitor)")
            rc = 1
            continue
        cf = ctl_path_for(path)
        try:
            seq = json.loads(cf.read_text()).get("seq", 0) + 1
        except (OSError, ValueError):
            seq = state.get("ctl_seq", 0) + 1
        tmp = cf.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"seq": seq, "changes": changes, "written": time.time()}))
        os.replace(tmp, cf)
        deadline = time.monotonic() + a.wait
        applied = False
        while time.monotonic() < deadline:
            try:
                if json.loads(state_path_for(path).read_text()).get("ctl_seq", 0) >= seq:
                    applied = True
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
        verdict = "applied" if applied else "sent; the watcher applies it at its next poll"
        print(f"bgwatch ctl: {path} (watcher pid {state['pid']}): {verdict}")
    return rc


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["ctl"]:
        return ctl_main(argv[1:])
    if argv[:1] == ["_cmdpoll"]:
        return cmd_poller_main(argv[1:])
    a = build_parser().parse_args(argv)
    if a.until and not a.cmd:
        build_parser().error("--until needs --cmd")
    if a.cmd:
        if a.file or a.pid or a.pgrep or a.slurm or a.once:
            build_parser().error("--cmd is the source: no file, --pid/--pgrep/--slurm or --once with it")
        if a.match:
            try:
                re.compile(a.match)
            except re.error as e:
                build_parser().error(f"--match: bad regex: {e}")
        a.file = str(start_cmd_poller(a))
        a.from_start, a.fresh = True, True
        a.match = a.match or "."
    if a.print_defaults:
        print(f"--fail   {DEFAULT_FAIL}")
        print(f"--ignore {DEFAULT_IGNORE}")
        return 0
    if a.no_fail:
        a.fail = None
    elif a.fail_also:
        a.fail = f"(?:{a.fail})|(?:{a.fail_also})"
    if not a.no_default_ignore:
        a.ignore = f"(?:{DEFAULT_IGNORE})|(?:{a.ignore})" if a.ignore else DEFAULT_IGNORE
    if not a.file and not (a.pid or a.pgrep or a.slurm):
        build_parser().error("give a file to follow, or --pid/--pgrep/--slurm")
    for name in ("every", "stall"):
        v = getattr(a, name)
        if v != "auto":
            try:
                float(v)
            except ValueError:
                build_parser().error(f"--{name}: expected seconds or 'auto', got {v!r}")
    for name in ("fail", "match", "ignore"):
        pat = getattr(a, name)
        if pat:
            try:
                re.compile(pat)
            except re.error as e:
                build_parser().error(f"--{name}: bad regex: {e}")
    return Watcher(a).run()


if __name__ == "__main__":
    sys.exit(main())
