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

**Bare task ids (same day, Clément's question "does the command need the full path?").**
`bgwatch bgt3ng9gt` resolves a harness task id under `<tmp>/claude-<uid>/*/*/tasks/`, so
the Monitor call the model retypes is ~80 characters shorter; the hint prints the id for
harness files and a cwd-relative path for a job's own log. `--once` now counts the file's
lines instead of reporting 0.

## 2026-09-14 — hook ablation

Clément asked whether the hint hook was needed or whether the CLAUDE.md paragraph alone
does it. Three more whowill arms on the opaque-manifest hang task (n=8): snippet-only
fable, snippet+hook opus, snippet-only opus. bgwatch in the Monitor call: snippet-only
5/8 (fable) and 7/8 (opus); with the hook 8/8 for both. Hang noticed: snippet-only fable
6/8, everything else 8/8. The snippet-only arms produced the two mistakes the hook
prevents (watching the harness task file while the job wrote its own log; no Monitor at
all). Decision: keep the hook in the adoption package. Full table in
`~/.claude/tools/whowill/REPORT.md` ("Ablation").

Also confirmed on the new defaults: hang noticed at a median 3.6–3.9 min in every arm
running the adaptive-stall bgwatch, versus 10.2 min on the fixed-default one.

## 2026-09-16 — a default ignore list (opus-5)

Clément: `--ignore "pending/completed/failed"` "is always there with inspect", make it a
default. The line is inspect_ai's batch-status poll, `print()`ed to stdout from
`model/_providers/util/batch_log.py`: `Current batches: 2, requests
(pending/completed/failed): 120/45/0, oldest batch age: 5m 12s`. Its `failed` token is
word-bounded so it hits DEFAULT_FAIL on every poll — a recurring `[fail]` notification
for a healthy run, which is the exact noise `--max-rate` then hides real failures behind.

`DEFAULT_IGNORE` is the literal `pending/completed/failed`, and `--ignore` EXTENDS it
(unlike `--fail`, which replaces). Asymmetric on purpose: the default ignore list is
known-benign noise, not a policy the caller is meant to re-state, and a user pattern
never needs to un-ignore it. `--no-default-ignore` is the escape hatch.

Deliberately kept to this one line. inspect's other always-printed strings that trip the
failure regex — `WARNING: N of M executed samples had errors`, `Task interrupted` — are
things you do want woken for.

## 2026-09-16 — `--probe CMD` (opus-5)

Asked for by a session babysitting an Anthropic batch run: the job's real progress signal
lived in the API, not in its log, so every heartbeat said `alive · 0 lines` and the
session listed batches by hand through the SDK at each check. `--probe CMD` runs a shell
command on every status line and appends its output, turning each wake into the number
the watcher actually wanted.

Decisions:

- Runs inside `status()`, so it rides on `[hb]`, `[STALL]` and `--once` — a stall is
  exactly when the outside-the-log number matters most. Not on `[EXIT]`: the job is over,
  a live-state query is likely meaningless there, and that line already carries the tail.
- Synchronous in the poll loop, capped by `--probe-timeout` (default 45 s). A thread
  would avoid delaying file reads by up to that much; not worth the machinery for a
  command that should be a single API call.
- A probe that exits non-zero or times out reports `probe rc=N: …` / `probe timed out`
  on the line instead of raising. A broken probe must never kill the watcher — that would
  trade a missing number for a leaked Monitor.
- Output is flattened to one line (`·`-joined) and clipped to 160 chars: every stdout line
  is a notification, so a chatty probe can't blow up the wake.

## 2026-09-25 — in-the-wild evaluation and the fixes it drove (opus-5-5)

An independent evaluation read every background launch in the archive from 08-01 to 09-25
(2,126 jobs; all 86 long work jobs read by eye), report at
`~/claude-playgrounds/archive-sweep-09-25/bgwatch-eval/report.md`. Verdict: more jobs are
watched (long work jobs with a Monitor 6/48 → 32/38) and watchers end with their jobs, but
waiting got more expensive (about 12 idle wakes per watched long job) and three blind spots
produced the worst episodes. Changes, each replayed against the real logs still on disk
(213 files, 109k lines; scripts `replay_regex.py`, `replay_hint.py` next to the report):

- *Log-shaped failure default.* Bare case-insensitive words fired on echoed model output,
  test names and code listings. Replay, distinct matched lines hand-labelled: old default
  329 matches = 135 real + 194 false; new 166 = 144 real + 22 false (the remaining false ones
  are CamelCase exception names inside echoed code, e.g. `raise ValueError(...)` in a
  generated answer, plus one Playwright teardown `TargetClosedError`). Lines only the old
  default caught that were real: 6 (`[ELIFECYCLE] Test failed.`, `failed: <names>`,
  `Failed Tests 1`), each in a file where the new default still fires on the same failure
  (`1 failed`, `Failed:`, `FAILED`), so no job's failure goes unreported. Lines only the new
  default catches that are real: 15, mostly exception lines printed outside a Python
  traceback (`RuntimeError: 0 valid draws…`, `IndexError: 2`) — `\berror\b` never matched
  `IndexError`. Classes kept on purpose although rare in the corpus: segfault / core dumped,
  command not found, no such file or directory, permission denied, slurmstepd / CANCELLED,
  `exit code N`, `timed out`, `Sample error` (inspect).
- *Default ignore list* grows by four always-benign shapes that the new default would still
  match: Modal's "Timed out waiting for final app logs", glog W/I-level lines (torch prints a
  "Traceback" inside warnings), rich-box source lines (`│ ❱ 550 │ raise …`), and the harness's
  `[exited with code N]` trailer (the completion notice and `[EXIT]` already carry it).
- *ANSI stripped* before matching and display (vitest colour codes hid `Error:` from `^`).
- *Stall floor 60 → 180 s.* Replay on the 11 auto `[STALL]`s in the archive: 2 were real
  hangs (8.8 and 10 min of silence) and still fire, 2 min later; 9 were benign pauses, of which
  3 (silences of 118, 119 s and one unknown) no longer fire and 6 (210–411 s) still do. A
  300 s floor would have dropped 3 more, at 4 min later detection; left at 180 for now. The
  cadence ratchet (one long gap raises the auto threshold to the 30-min cap) is unchanged:
  replaying it needs per-line timestamps the archive doesn't have.
- *Young jobs read from the top.* Instances arm the Monitor a median 5 s (p90 19 s) after the
  launch, and bgwatch started at the end of the file: a typecheck failure printed in those
  seconds was never reported (51e9ec87:778). Now, when the file's holder started under 2 min
  ago and the file holds only this job's output (a harness task file, or a holder that did not
  open it O_APPEND — the harness opens task files O_APPEND, a `>` redirect doesn't, `>>` does),
  reading starts at the top.
- *Wrong-file pointer.* Blind watches (task id watched while the command redirected to its
  own log, every heartbeat "0 lines") were the costliest misuse: one session made about 40
  manual checks and 15 sleep timers. While the watched file stays empty, the first status line
  names the regular files the job's process tree has open for writing, once.
- *`--every 0`* turns heartbeats off (an instance wanted a watcher that "wakes on stop/failure
  only" and got 12 heartbeat wakes). `[resumed]` now reads "new output after M:SS of silence
  (the job is still running)" — it was read as "the batch returned" once. Heartbeats say
  "N new lines (file had X kB before bgwatch attached)" when the file wasn't empty.

Hint hook (both copies): the target parser ignored quoting and heredocs and didn't expand
variables, so 161 of 574 real hints (28%) named an unusable file (`bgwatch :`, `bgwatch {`,
`bgwatch $SD/push.log`), and opus instances fell back to the task id, which is empty when the
command redirects. Now heredoc bodies and quoted strings are skipped, `cat > f` / `echo … > f`
style writes don't count, `NAME=value` set earlier in the command (and the environment) are
expanded, and a target that still has a `$` produces an explicit "give its absolute path, not
the task id" instead of a broken call. Replay over the full commands: unusable targets
161 → 4 (3 of them now carry that explicit instruction). The alternative "(or the harness task
file)" is replaced by the reason not to use it, and the hint says servers and tunnels need no
watcher.

## 2026-09-25 (later) — the hint's own volume

The hint injected about 1,000 characters on every background launch: 572 launches in 60
sessions since 09-14, most of them jobs that end within a minute or two. Two changes, in both
copies of the hook, replayed over those sessions (`replay_hint_volume.py` next to the
evaluation report):

- The full explanation goes out on the first launch of a session. Later launches get one line
  that still carries the exact Monitor call, the resolved target and, for a redirect, the
  reason not to use the task id. Per-session state is a small JSON file in
  `<tmp>/claude-<uid>/bgwatch_hint/`; losing it only repeats the full hint.
- A command that the sync timeout moved to the background, rather than one launched with
  `run_in_background`, gets no hint at launch. Replay: 68 of 76 such commands ended within
  2 min of starting. If it is still running 2 min after it started (its task file is still held
  open) and no bgwatch process already names it, the hint rides on the next Bash call.

Result: characters injected went from 577k to 166k (29%). Of the 8 auto-backgrounded commands
that ran past 2 min, 5 would get the deferred hint, a mean 149 s after they started. The other
3 saw no Bash call before they ended. None of the 8 had a watcher armed under the old hint.

Gap: hooks only run on tool calls, so a deferred hint can't reach an idle model. The job's
completion notification still does. What the replay can't show is whether the one-line form
keeps adoption as high as the full hint did; a whowill arm would measure that.

## 2026-09-25 (night) — two more failure classes, from a wider replay

Replaying the new default over every task file on disk (1,175 job outputs, not only the long
jobs) turned up real failure lines it missed. There were two shapes:
- a program's own lower-case report: `runpod_session: setup failed on pod …`,
  `mats_render: render failed on job …`;
- an error payload: `{'error': 'dedup call failed'}`, `{"error": "The encrypted content …"}`.

Both are now alternatives (`^[\w.-]+: .*\bfailed\b`, `['"]error['"]\s*:\s*['"]`). Replay
against the previous default (`replay/fail_regex.py --baseline main`): +5 real lines, 0 false,
0 lines lost. `Terminated` was tried and dropped: 1 real hit (a command killed by `timeout`,
which the exit code already reports) against 3 intentional `pkill`s of helper processes.
