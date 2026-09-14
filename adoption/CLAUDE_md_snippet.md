
## Watching a background job: `bgwatch`

A background launch that will run more than a couple of minutes gets one persistent Monitor on its output file, armed right after the launch (the launch hook prints the exact call):

```
Monitor(command="bgwatch <task-id or log file>", persistent=true, description="<what the job is>")
```

`bgwatch` wakes you only for: failure lines (Traceback, Error, Killed, OOM, … — `--fail RE` replaces the default, `--fail-also RE` extends it, `--ignore RE` drops lines), your `--match RE` progress marker, a heartbeat backing off from 1 to 10 min (`--every S` to fix it), a stall warning scaled to the job's own output cadence (`--stall S` to fix it), and an `[EXIT]` line with the tail when the job ends — then it exits, so `persistent: true` never leaks. Job end is detected because the job holds the watched file open (no PID or `--pgrep` needed when the job writes that file); `--pid N` / `--pgrep PATTERN` / `--slurm JOBID` only for a job that doesn't. `bgwatch --print-defaults` shows the default regex; `bgwatch --once FILE|ID` is the one-line status check to use instead of `cat`-ing the file.

No separate ScheduleWakeup / cron on top: the heartbeat is the backstop for a hung job, and a dead bgwatch is reported by Monitor as an exit. The exception is a multi-hour babysit that must survive `/clear` or a CLI restart, which kill every Monitor with the session — add a cron health check there. Jobs that finish in seconds need nothing: the completion notification covers them. Monitor is a deferred tool: `ToolSearch select:Monitor` once per session if it isn't loaded.
