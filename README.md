# bgwatch

The Monitor command for anything long-running. One call, no timeout to guess, no grep to
get right:

```
Monitor(command="bgwatch /path/to/task.output", persistent=true, description="training run")
```

It follows the file and prints one line per thing worth waking up for. Every stdout line
is a Monitor notification, so it is deliberately sparse:

| line | when |
|---|---|
| `[fail] …` | a line matches the failure regex (Traceback, Error, Killed, OOM, …). A Python traceback is folded into one line: `Traceback → KeyError: 'labels'` |
| `[match] …` | a line matches your `--match` (progress / success marker) |
| `[hb 0:10:00] alive · 1234 lines · last: …` | heartbeat; by default the interval backs off 1 → 2 → 4 → 8 → 10 min (`--every S` fixes it, `--every-min/--every-max` bound the backoff) |
| `[STALL no output for 31:00 …]` | silence longer than the job's own cadence: by default 5× the longest gap seen so far, clamped to 1–30 min (`--stall S` fixes it, `0` disables; `--stall-min/--stall-max` bound auto); `[resumed]` when it writes again |
| `[EXIT 1:23:45] job ended (pid 123) · 5678 lines · 1 fail line seen` + the last `--tail` lines | the job ended. bgwatch exits here, so `persistent: true` never leaks |

**Job end without a PID.** A background task's process holds its output file open on
fd 1/2, so bgwatch scans `/proc/*/fd` for the file (verified on the harness's tasks,
2026-09-14). For jobs that don't hold the file — a slurm job, a server started elsewhere,
a process that daemonizes — give it `--pid N`, `--pgrep PATTERN` or `--slurm JOBID`. `--pgrep` ignores bgwatch itself and every ancestor process (the harness runs a Monitor command through a `bash -c` wrapper whose command line contains the pattern).
If nothing holds the file when bgwatch starts (after `--grace` seconds) it says so and
keeps watching without an exit condition.

**Patterns are yours.** `--fail RE` replaces the default failure regex, `--fail-also RE`
extends it, `--ignore RE` drops lines before any matching, `--no-fail` turns it off.
`bgwatch --print-defaults` prints the default. The default is word-bounded and
case-insensitive, so `error_rate=0.02` and `errors=0` do not match but `Error:` and
`loss=nan` do.

**Volume.** `--max-rate N` (default 20/min) caps `[fail]`/`[match]` lines; the excess is
counted and reported once a minute. Each notification costs ~700 chars of context
(the harness prepends a fixed preamble); the backoff keeps a 4-hour job at ~25 heartbeats.

**Instead of `cat`-ing the file:** `bgwatch --once FILE` prints one status line
(alive?, lines, last line) and exits.

```
bgwatch /tmp/.../tasks/b1.output                       # harness background task
bgwatch train.log --match 'step \d+00 ' --every 900    # progress every 100 steps + 15-min heartbeat
bgwatch train.log --ignore 'error_rate=' --fail-also 'loss=nan|diverged'
bgwatch slurm-44297.out --slurm 44297                  # slurm: squeue/sacct decide "ended"
bgwatch server.log --pgrep 'vllm serve' --fail 'CUDA|Killed'
bgwatch --once train.log
```

No separate ScheduleWakeup / cron is needed: the heartbeat is the backstop for a hung
job, and Monitor reports bgwatch's own exit if it dies. The one case a cron health check
still earns its place is a multi-hour babysit that must survive `/clear` or a CLI
restart, which kill every Monitor with the session.

## Why it exists

Mined from ~12k sessions on 2026-09-14 (see ENGINEERING_LOGS.md): Monitor went from the
first choice after 43% of background launches to under 3%, replaced by blind timers;
of the 654 Monitor commands ever written 65% were unbounded *and* non-persistent (they
could only end by timing out, and 84% of the 198 timeouts were never re-armed), 10%
had a grep without `--line-buffered` (matches never delivered), 9% matched only the
success path. The four decisions a hand-written watcher needs — pattern, buffering,
timeout, exit condition — are what instances skipped. bgwatch makes them defaults.

## Tests

```
cd ~/.claude/tools/bgwatch && python3 -m pytest tests -q
```

`tests/fake_job.py` is a job that logs progress, prints a traceback mid-way but keeps
running, optionally stalls, then exits — the same job the whowill adoption experiment
uses.
