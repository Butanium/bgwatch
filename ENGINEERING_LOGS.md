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

## 2026-09-14 (later) — first whowill A/B, and what it changed

Task A (8 control vs 8 treatment fable sessions, 4-min job that prints a traceback at
65 s, keeps running, exits 0; prompt says "tell me as soon as anything goes wrong"): the
hook's ready-made call is used 8/8, but control already armed a Monitor 8/8 and reported
the error within ~1.3 min 8/8 — with an explicit early-warning request this task is a
ceiling for the current CLAUDE.md. What the treatment removed is the cost: zero
fallback `sleep && echo` timers (control 6/8, each later needing ToolSearch + TaskStop),
no hand-written poll loops (control wrote 8 different ones), fewer tool calls (median 9
vs 11). One control watcher died early (`tail -f --pid=$(pgrep …)` matched the wrong pid);
the fd-holder detection is exactly what that is for. Full report: `~/.claude/tools/whowill/REPORT.md`.
Task B (no early-warning request, job hangs) is the discriminating run; results there.

Changes driven by the A/B (dev worktree, merged after the task-B runs finished so the
PATH copy stayed frozen during the experiment):

- `--pgrep` now excludes bgwatch's whole ancestor chain. The harness runs a Monitor
  command as `bash -c "source … && bgwatch … --pgrep PAT"`, so the wrapper's own
  command line matched PAT: 3/8 treatment sessions that added `--pgrep` got a false
  `[STALL] … alive` after the job had exited. The hint no longer suggests `--pgrep`
  and says why it isn't needed when the job writes the watched file.
- Adaptive defaults. Instances overrode `--stall 60` for a 4-minute job because the
  fixed defaults (10-min heartbeat, 30-min stall) are sized for hour-scale jobs.
  Now `--every auto` backs off 1→2→4→8→10 min and `--stall auto` is 5× the longest
  inter-line gap the job has shown, clamped to 1–30 min (30 min before the job has
  printed twice). Fixed values still accepted. `--poll S` sets the file poll interval,
  which is also the granularity of the cadence estimate (tests use 0.1).
- The hint names the job's own log when the command redirects stdout to a file
  (`> job.log 2>&1`, with a leading `cd DIR &&` resolved): every treatment session
  re-pointed the hint from the harness task file to `job.log` by hand.
- `[EXIT]` delivery was briefly suspected lost; it lands in the same instant as the
  harness completion notification and is absorbed mid-turn (a `queued_command`
  attachment in the transcript, not a user row). Not a bug.

## 2026-09-14 — Task B (hang) results, and the defaults question

Four runs in total (`~/.claude/tools/whowill/REPORT.md`, fable in every arm, n=8 per arm).
Task B: same job but no early-warning request, a shard error at 65 s and a hang at 150 s
with no completion notification ever. Control's Monitor use dropped to 3/8 (hang-01) and
6/8 (hang-02, opaque manifest); the shard error was reported by 5/8 in hang-01 versus
8/8 in treatment, a median 4.0 min after launch versus 1.2. Across all 32 treatment
sessions: bgwatch in the Monitor call 32/32 (never verbatim — all re-targeted the job's
own log), fallback `sleep` timers 0/32 versus 14/32 in control, false completions 0/64.

What did not improve: with the *old* fixed defaults (10-min heartbeat, 30-min stall) both
arms noticed the hang at ~10 min — treatment from the heartbeat, control from the CLI's
own `[Monitor timed out — re-arm if needed.]` at its 10-min default, which three control
sessions per run read as "should be done by now, let me look". Treatment sessions that set
`--stall 60–120` on their own noticed at 3.5–4.5 min; nothing told them to. The adaptive
stall merged today (5× the job's longest gap, floor 1 min) would have fired about one
minute into the hang for this 5-second-cadence job without any flag. Run-to-run variance at
n=8 is large (control Monitor use 3/8 → 6/8 with only the manifest format changed);
between-arm gaps under ~3/8 are noise.

Follow-ups taken from the report: heartbeat prints idle time explicitly; `--pgrep` is a
fallback behind fd-holder detection rather than an override (10/32 treatment sessions
added it although the default would have found the pid).
