
## Watching a background job: `bgwatch`

A background launch that will run more than a couple of minutes gets one persistent Monitor on its output file, armed right after the launch (the launch hook prints the exact call):

```
Monitor(command="bgwatch <output-file>", persistent=true, description="<what the job is>")
```

`bgwatch` wakes you only for: failure lines (Traceback, Error, Killed, OOM, … — `--fail RE` replaces the default, `--fail-also RE` extends it, `--ignore RE` drops lines), your `--match RE` progress marker, a heartbeat every 10 min (`--every S`), a stall warning (`--stall S`), and an `[EXIT]` line with the tail when the job ends — then it exits, so `persistent: true` never leaks. Job end is read from `/proc` (no PID needed); `--pid N` / `--pgrep PATTERN` / `--slurm JOBID` for jobs that don't hold the file. `bgwatch --print-defaults` shows the default regex; `bgwatch --once FILE` is the one-line status check to use instead of `cat`-ing the file.

No separate ScheduleWakeup / cron on top: the heartbeat is the backstop for a hung job, and a dead bgwatch is reported by Monitor as an exit. The exception is a multi-hour babysit that must survive `/clear` or a CLI restart, which kill every Monitor with the session — add a cron health check there. Jobs that finish in seconds need nothing: the completion notification covers them. Monitor is a deferred tool: `ToolSearch select:Monitor` once per session if it isn't loaded.
