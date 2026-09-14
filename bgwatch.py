#!/usr/bin/env python3
"""bgwatch — the Monitor command for anything long-running.

    Monitor(command="bgwatch /path/to/task.output", persistent=true, description="...")

Follows a log file and prints one line per thing worth waking up for:

  [fail]  a line matching the failure pattern (Traceback, Error, Killed, OOM, ...)
  [match] a line matching --match (your progress / success marker)
  [hb]    a heartbeat every --every seconds: alive?, line count, last line
  [STALL] nothing written for --stall seconds
  [EXIT]  the job ended — then bgwatch exits, so `persistent: true` never leaks

Job end is detected without a PID: a background task's process holds its output
file open on fd 1/2, so bgwatch scans /proc/*/fd for the file. `--pid`, `--pgrep`
and `--slurm` cover jobs whose output file isn't held open by the job itself.

Every stdout line is a notification, so the output is deliberately sparse.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Scoped (?i:...) rather than a global (?i) so the pattern can be OR-ed with --fail-also.
DEFAULT_FAIL = (
    r"(?i:\b(traceback|error|exception|failed|failure|fatal|killed|oom|out of memory"
    r"|segmentation fault|core dumped|cuda error|assert(ion)?|nan|timed? ?out"
    r"|command not found|no such file|permission denied|cancelled|slurmstepd"
    r"|exit(ed)? (code|status) [1-9])\b)"
)
TRACEBACK_HEAD = re.compile(r"^Traceback \(most recent call last\)")
LAST_LINE_MAX = 200
POLL_S = 1.0


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


class FdHolderJob(Job):
    """Alive while some process other than us holds the file open."""

    kind = "fd-holder"

    def __init__(self, path: Path):
        self.target = str(path.resolve())
        self.me = os.getpid()
        self.pids: set[int] = set()

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


# ------------------------------------------------------------------- watcher


class Watcher:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.path = Path(a.file).expanduser() if a.file else None
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

    def _make_job(self) -> Job:
        a = self.a
        if a.pid:
            return PidJob(a.pid)
        if a.pgrep:
            return PgrepJob(a.pgrep)
        if a.slurm:
            return SlurmJob(a.slurm)
        if self.path:
            return FdHolderJob(self.path)
        return Job()

    # ---- line handling
    def _remember(self, line: str) -> None:
        now = time.monotonic()
        if self.lines:
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
            emit(f"[resumed {hms(self.elapsed)}] output resumed · {clip(line)}")

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
        if self._rate_ok():
            emit(f"[{tag}] {clip(text)}")

    def handle(self, line: str) -> None:
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
        if self.a.every == "auto":
            self.hb_interval = min(cur * 2, self.a.every_max)
        return cur

    def describe_defaults(self) -> str:
        a = self.a
        hb = f"{hms(a.every_min)}→{hms(a.every_max)} backoff" if a.every == "auto" else f"every {hms(float(a.every))}"
        if a.stall == "auto":
            st = f"auto (5x the job's own cadence, {hms(a.stall_min)}–{hms(a.stall_max)})"
        else:
            st = f"after {hms(float(a.stall))}" if float(a.stall) else "off"
        return f"heartbeat {hb} · stall {st}"

    # ---- status lines
    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def status(self, tag: str, alive: bool | None, note: str = "") -> str:
        state = {True: "alive", False: "ended", None: "job state unknown"}[alive]
        parts = [note] if note else []
        parts.append(state)
        if self.path:
            parts.append(f"{self.lines} lines")
        if self.fail_count:
            parts.append(f"{self.fail_count} fail line{'s' if self.fail_count > 1 else ''} so far")
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
        if a.once:
            emit(self.status("status", alive if self.job.seen else None))
            return 0
        detect = "off (no process holds the file; use --pid/--pgrep/--slurm)" if (self.job.kind == "fd-holder" and not self.job.seen) else self.job.describe()
        emit(
            f"[bgwatch] watching {self.path or self.job.describe()} · job-end detection: {detect}"
            f" · {self.describe_defaults()}"
        )
        if f and not a.from_start:
            f.seek(0, os.SEEK_END)
        next_hb = time.monotonic() + self.next_hb_interval()
        next_job_check = time.monotonic() + a.check_every
        job_alive = alive
        while True:
            progressed = False
            if f:
                progressed = self._drain(f)
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
            thr = self.stall_threshold
            if thr and not self.stalled and now - self.last_write >= thr and (job_alive or job_alive is None):
                self.stalled = True
                emit(self.status("STALL", job_alive, note=f"no output for {hms(now - self.last_write)}"))
            if now >= next_hb:
                emit(self.status("hb", job_alive if self.job.seen else None))
                next_hb = now + self.next_hb_interval()
            if not progressed:
                time.sleep(a.poll)

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
            "drops lines (checked first). `bgwatch --print-defaults` shows the default regex.\n"
            "examples:\n"
            "  bgwatch /tmp/.../tasks/b1.output                     # harness background task\n"
            "  bgwatch train.log --match 'step \\d+00 ' --every 900   # progress every 100 steps, fixed 15-min heartbeat\n"
            "  bgwatch train.log --ignore 'error_rate=' --fail-also 'loss=nan|diverged'\n"
            "  bgwatch slurm-44297.out --slurm 44297\n"
            "  bgwatch server.log --pgrep 'vllm serve' --fail 'CUDA|Killed'\n"
            "  bgwatch --once train.log                             # one status line, then exit"
        ),
    )
    p.add_argument("file", nargs="?", help="log / task output file to follow")
    p.add_argument("--every", default="auto", metavar="S|auto", help="heartbeat interval in seconds; auto (default) backs off from --every-min to --every-max")
    p.add_argument("--every-min", type=float, default=60, metavar="S", help="first heartbeat interval in auto mode (default 60)")
    p.add_argument("--every-max", type=float, default=600, metavar="S", help="heartbeat interval cap in auto mode (default 600)")
    p.add_argument("--stall", default="auto", metavar="S|auto", help="warn when nothing was written for S seconds (0 = off); auto (default) = 5x the job's longest gap so far, clamped to [--stall-min, --stall-max]")
    p.add_argument("--stall-min", type=float, default=60, metavar="S", help="auto stall floor (default 60)")
    p.add_argument("--stall-max", type=float, default=1800, metavar="S", help="auto stall cap, also the threshold before the job has printed twice (default 1800)")
    p.add_argument("--match", metavar="RE", help="also emit lines matching this regex (progress / success markers)")
    p.add_argument("--fail", metavar="RE", default=DEFAULT_FAIL, help="failure regex; replaces the default")
    p.add_argument("--fail-also", metavar="RE", help="extend the failure regex with this alternative")
    p.add_argument("--no-fail", action="store_true", help="disable the failure regex entirely (only --match, heartbeats, exit)")
    p.add_argument("--ignore", metavar="RE", help="never emit lines matching this (checked before --fail/--match)")
    p.add_argument("--pid", type=int, help="the job is this pid")
    p.add_argument("--pgrep", metavar="PATTERN", help="the job is `pgrep -f PATTERN`")
    p.add_argument("--slurm", metavar="JOBID", help="the job is this slurm job (squeue/sacct)")
    p.add_argument("--from-start", action="store_true", help="scan the existing content too (default: start at the end)")
    p.add_argument("--tail", type=int, default=5, metavar="N", help="lines of tail in the EXIT summary (default 5)")
    p.add_argument("--max-rate", type=int, default=20, metavar="N", help="max [fail]/[match] lines per minute (default 20)")
    p.add_argument("--grace", type=float, default=15, metavar="S", help="seconds to wait for the file / the job to appear (default 15)")
    p.add_argument("--check-every", type=float, default=5, metavar="S", help="job liveness poll interval (default 5)")
    p.add_argument("--poll", type=float, default=POLL_S, metavar="S", help="file poll interval (default 1); also the granularity of the auto stall cadence estimate")
    p.add_argument("--no-exit", action="store_true", help="keep following the file after the job ends")
    p.add_argument("--once", action="store_true", help="print one status line and exit (a poll that isn't a cat)")
    p.add_argument("--print-defaults", action="store_true", help="print the default failure regex and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.print_defaults:
        print(DEFAULT_FAIL)
        return 0
    if a.no_fail:
        a.fail = None
    elif a.fail_also:
        a.fail = f"(?:{a.fail})|(?:{a.fail_also})"
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
