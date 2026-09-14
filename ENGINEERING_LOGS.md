# bgwatch — engineering log

## 2026-09-14 — origin (fable-5.1, session 86a67a81)

Clément asked whether the way instances run and watch background jobs could be improved
by tooling, the way `whowas` changed how often they consult the archive. Mined the
whowas index (12,230 files, 768k messages) before designing anything.

**What the archive showed.**

- ~900 explicit `run_in_background` launches a month; jobs over 5 min are ~15% of them.
- Monitor as the first wait strategy after a launch: 43% in April (opus-4.7), 22% May,
  ≤3% since June. Replaced by ScheduleWakeup/cron ticks and background `sleep N && echo`
  timers — blind polls that miss an early crash for a whole interval.
- Of 654 Monitor calls ever made: 77% unbounded command (`tail -f`, `while true`), 65%
  unbounded AND non-persistent → can only end by timeout. 198 "Monitor timed out —
  re-arm if needed" notifications; 84% never re-armed. 10% of grep pipelines without
  `--line-buffered`; 9% match only a success marker. Timeout mode = the 60-min cap.
- Jobs ≥30 min: about a third had any watcher. Clément's nags are the ground truth
  ("you don't have monitoring jobs how do you plan to get notified when they finish?",
  2026-05-19; "do you have a scheduled wakeup / a loop on top of the monitor?",
  2026-05-22).
- Poll mania (chains of ≥3 consecutive polls) is 4% of wait chains and the
  `no_poll_background` hook force-stops the worst; not the live problem.
- One documented persistent-Monitor death (2026-07-28, after `/clear`): the backstop
  advice in CLAUDE.md has a real trigger, but a rare one.

**Design decisions.**

- *Job-end detection via `/proc/*/fd`.* The harness's background task holds its output
  file open on fd 1 and 2 (both the `bash -c` wrapper and the child; verified live).
  This is what lets bgwatch exit with the job, which is what makes `persistent: true`
  always correct and removes the timeout decision. Alternatives rejected: `--pid` only
  (the harness never exposes the pid), inotify (says "written", not "ended").
- *Traceback folding.* The default regex matches the `Traceback` header and the final
  `XError:` line; emitting both is two notifications for one event. bgwatch buffers the
  indented frames and emits `Traceback → KeyError: 'labels'` once.
- *Pattern control is the model's* (Clément's ask): `--fail` replaces, `--fail-also`
  extends, `--ignore` drops first, `--no-fail` disables. Defaults are word-bounded so
  `error_rate=` doesn't fire.
- *Heartbeat inside the watcher* rather than a separate ScheduleWakeup: one primitive,
  and silence is never ambiguous (a heartbeat with `+0 lines` is a stall, a missing
  heartbeat means the Monitor itself is gone and Monitor reports that exit).
- Pure Python follow loop, no `tail` subprocess: line buffering and partial-line handling
  are under our control, and there is no pipe stage to forget `--line-buffered` on.
- `--slurm` is written from the standard `squeue -h -j ID -o %T` / `sacct -n -X -j ID -o
  State` shapes and is untested on this box (no slurm).

**Adoption package** (`adoption/`): a PostToolUse hook that, on a background launch,
injects the exact ready-to-paste Monitor call for that task's output file; a CLAUDE.md
snippet replacing the Monitor/cron/sleep bullets. Tested by the whowill A/B (control =
current config, treatment = this package) — results in whowill's run dir and in this log
once available.

**Gotcha (same day).** The default failure regex started with a global `(?i)`; wrapping it
in `(?:…)|(?:extra)` for `--fail-also` is a Python `re` error ("global flags not at the
start of the expression"). The default now uses a scoped `(?i:…)` group, so user patterns
keep their own case-sensitivity when OR-ed in. Caught by `test_ignore_and_fail_also_and_match`.

**PostToolUse payload for a background launch** (probed via `claude -p --settings` with a
stdin-dumping hook, 2.1.257): `tool_response.backgroundTaskId`, `session_id`, `cwd`,
`transcript_path` — no output path. It is `<tmp>/claude-<uid>/<cwd slug>/<session>/tasks/<id>.output`
(slug = cwd with `/` and `.` → `-`); the hint hook globs for it and falls back to the computed path.
