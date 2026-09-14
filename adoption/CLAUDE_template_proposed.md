# Proposed edit to ~/.claude/CLAUDE.template.md (not applied — needs Clément's ✓)

Replace the four "Watching progress" bullets (template lines 281–284 as of 2026-09-14) with:

```
  - **Watching progress — `bgwatch`.** A background launch that will run more than a couple of minutes gets one persistent Monitor on its output file, armed right after the launch (the launch hook prints the exact call):
    `Monitor(command="bgwatch <output-file>", persistent=true, description="<what the job is>")`
    bgwatch wakes you only for failure lines (Traceback, Error, Killed, OOM… — `--fail RE` replaces the default, `--fail-also RE` extends it, `--ignore RE` drops lines), your `--match RE` progress marker, a 10-min heartbeat (`--every S`), a stall warning (`--stall S`), and an `[EXIT]` line with the tail when the job ends — then it exits, so `persistent: true` never leaks. Job end is read from `/proc` (no PID needed); `--pid N` / `--pgrep PATTERN` / `--slurm JOBID` for jobs that don't hold the file. `bgwatch --once FILE` is the one-line status check to use instead of `cat`-ing the file. No ScheduleWakeup / cron on top: the heartbeat is the backstop for a hung job and Monitor reports a dead bgwatch as an exit. Exception: a multi-hour babysit that must survive `/clear` or a CLI restart (they kill every Monitor) — add a cron health check there. Jobs that finish in seconds need nothing; the completion notification covers them. Monitor is a deferred tool: `ToolSearch select:Monitor` once per session if it isn't loaded.
  - **One-off "remind me in N seconds"** (not job-watching): `sleep N && echo "check X"` with `run_in_background: true`, or `ScheduleWakeup`. **Recurring check-ins with no file to watch**: `CronCreate`.
```

Also in `hooks/force_background_bash.py` (sync-timeout clamp message) and `force_background_sleep.py`: replace "monitor it properly with Monitor or /loop" with "arm `bgwatch` on it (the launch hook prints the call)". And register `hooks/bgwatch_hint.py` in settings.json (`adoption/settings_hooks_fragment.json`).
