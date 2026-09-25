"""Tests for the changes driven by the 2026-09-25 archive evaluation: log-shaped default
failure regex, heartbeats that can be turned off, a young job's file read from the top,
the wrong-file pointer, ANSI stripping, clearer [resumed] and heartbeat wording."""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
BGWATCH = HERE.parent / "bgwatch.py"
FAKE_JOB = HERE / "fake_job.py"


def run_watch(args, timeout=40):
    p = subprocess.run([sys.executable, str(BGWATCH), *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.splitlines()


def writer(log: Path, lines, dt=0.1, hold=1.0, mode="w"):
    """A job that prints `lines` into `log` (holding it open), then stays alive `hold` s."""
    code = ("import sys, time\n"
            f"for l in {list(lines)!r}:\n    print(l, flush=True); time.sleep({dt})\n"
            f"time.sleep({hold})\n")
    f = open(log, mode)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=f, stderr=subprocess.STDOUT)
    f.close()
    return proc


def fails(lines):
    return [l for l in lines if l.startswith("[fail]")]


def test_default_fail_is_log_shaped(tmp_path):
    benign = [
        "The main error is that the verb doesn't agree.",       # echoed model output
        "if (num < 0) return NaN;",                              # echoed code
        "Who killed the millionaire?",
        "2 passed, 0 failed",
        "10 artifacts checked · FAILED: none",
        "  timeout = 5.0",
        "[modal-client] Timed out waiting for final app logs.",
        "(Worker_TP3 pid=88) [rank3]:W0918 01:15:18.083000 88 site-packages/torch/_inductor/triton_bundler.py:242] "
        "Traceback (most recent call last):",
        "log reads recovered after 1 failure(s)",
        "step 3 loss=0.97 error_rate=0.01",
        "charts: 15 errors: none",
        "│ ❱ 550 │   │   raise RemoteError(result.exception)   │",
        "[exited with code 144]",
    ]
    real = [
        "ERROR  run failed: command exited (1)",
        "3 passed, 2 failed",
        "FAILED tests/test_x.py::test_y",
        "scout:typecheck: a.ts(94,11): error TS2322: Type 'x' is not assignable",
        "srun: error: Socket timed out on send/recv operation",
        "fatal: Exiting because of an unresolved conflict.",
        "Runner killed (SIGKILL), exit code: 137.",
        "run.sh: line 12: 2824380 Killed                     python train.py",
        "Runner failed with exception: Worker disappeared",
        "Sample error (id: p3, epoch: 1):",
        "torch.OutOfMemoryError: CUDA out of memory.",
        "step 7 loss=nan",
        "[ELIFECYCLE] Command failed with exit code 1.",
    ]
    log = tmp_path / "job.log"
    proc = writer(log, benign + real, dt=0.02, hold=0.5)
    rc, lines = run_watch([str(log), "--from-start", "--every", "60", "--stall", "0", "--check-every", "0.3"])
    proc.wait()
    got = [l[len("[fail] "):] for l in fails(lines)]
    assert got == real, lines


def test_ansi_is_stripped(tmp_path):
    log = tmp_path / "job.log"
    proc = writer(log, ["scout:test: \x1b[31m\x1b[1mError\x1b[22m: Test timed out in 5000ms."], hold=0.5)
    rc, lines = run_watch([str(log), "--from-start", "--every", "60", "--stall", "0", "--check-every", "0.3"])
    proc.wait()
    assert fails(lines) == ["[fail] scout:test: Error: Test timed out in 5000ms."], lines


def test_every_zero_turns_heartbeats_off(tmp_path):
    log = tmp_path / "job.log"
    proc = writer(log, ["a", "b"], dt=0.2, hold=3)
    rc, lines = run_watch([str(log), "--every", "0", "--stall", "0", "--check-every", "0.3"])
    proc.wait()
    assert rc == 0 and "heartbeat off" in lines[0], lines
    assert not any(l.startswith("[hb") for l in lines), lines


def test_young_job_file_read_from_the_top(tmp_path):
    """The failure is printed before the watcher attaches (instances arm it ~5 s after launch)."""
    log = tmp_path / "job.log"
    proc = writer(log, ["Traceback (most recent call last):", '  File "x.py", line 1', "KeyError: 'labels'"], hold=3)
    time.sleep(1.5)
    rc, lines = run_watch([str(log), "--every", "60", "--stall", "0", "--check-every", "0.3"])
    proc.wait()
    assert fails(lines) == ["[fail] Traceback → KeyError: 'labels'"], lines


def test_appended_log_still_starts_at_the_end(tmp_path):
    """`>>` onto an old log: its history is not this job's, so don't replay it."""
    log = tmp_path / "job.log"
    log.write_text("ERROR: from yesterday's run\n")
    proc = writer(log, ["step 1"], hold=3, mode="a")
    time.sleep(1)
    rc, lines = run_watch([str(log), "--every", "1", "--stall", "0", "--check-every", "0.3"])
    proc.wait()
    assert not fails(lines), lines
    assert any("new lines (file had" in l for l in lines if l.startswith("[hb")), lines


def test_points_at_the_file_the_job_really_writes(tmp_path):
    """A task-id watch on `cmd > own.log`: the watched file stays empty, the heartbeat says where output went."""
    watched, own = tmp_path / "task.output", tmp_path / "own.log"
    job = f"{sys.executable} {FAKE_JOB} --steps 8 --dt 0.4 --crash-at 99 > {own} 2>&1"
    f = open(watched, "w")
    proc = subprocess.Popen(["bash", "-c", job], stdout=f, stderr=subprocess.STDOUT)
    f.close()
    rc, lines = run_watch([str(watched), "--every", "1", "--stall", "0", "--check-every", "0.3"])
    proc.wait()
    hbs = [l for l in lines if l.startswith("[hb")]
    assert any(str(own) in l and "re-arm" in l for l in hbs), lines
    assert sum(str(own) in l for l in hbs) == 1, lines  # said once, not on every heartbeat


def test_resumed_says_the_job_is_still_running(tmp_path):
    log = tmp_path / "job.log"
    proc = subprocess.Popen([sys.executable, str(FAKE_JOB), "--steps", "3", "--dt", "0.2", "--crash-at", "1", "--stall", "2.5"],
                            stdout=open(log, "w"), stderr=subprocess.STDOUT)
    rc, lines = run_watch([str(log), "--from-start", "--every", "60", "--stall", "1", "--check-every", "0.5"])
    proc.wait()
    res = [l for l in lines if l.startswith("[resumed")]
    assert res and re.search(r"new output after \d+:\d\d of silence \(the job is still running\)", res[0]), lines


HOOK = HERE.parent / "adoption" / "hooks" / "bgwatch_hint.py"


def call_hook(tmp_path, command, session="sess-1", task="babc123", background=True, timed_out_ms=None):
    """Run the hint hook once; state lives under TMPDIR=tmp_path. Returns the injected text or ''."""
    import json
    resp = {"stdout": "", "stderr": ""}
    if task:
        resp["backgroundTaskId"] = task
    if timed_out_ms:
        resp["timedOutAfterMs"] = timed_out_ms
    tool_input = {"command": command, "description": "job"}
    if background:
        tool_input["run_in_background"] = True
    payload = {"session_id": session, "cwd": str(tmp_path), "tool_name": "Bash", "tool_input": tool_input, "tool_response": resp}
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                       env={**os.environ, "TMPDIR": str(tmp_path)})
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"] if p.stdout.strip() else ""


def hint_for(command, tmp_path):
    """The full (first-in-session) hint for `command`, in a fresh session."""
    import uuid
    return call_hook(tmp_path, command, session=f"s-{uuid.uuid4().hex[:8]}")


def test_hint_ignores_heredocs_quotes_and_file_writers(tmp_path):
    # a `>` inside a heredoc body or a quoted string is code, not the job's redirect
    ctx = hint_for("python3 - <<'EOF'\nif a > b:\n    print(x)\nEOF", tmp_path)
    assert 'bgwatch babc123"' in ctx, ctx
    ctx = hint_for("sed 's/<a>[^<]*<\\/a>//' in.txt | wc -l", tmp_path)
    assert 'bgwatch babc123"' in ctx, ctx
    # `cat > run.py <<EOF` writes the script; the job's own log comes after it
    ctx = hint_for("cat > run.py <<'EOF'\nprint(1 > 0)\nEOF\npython run.py > logs/run.log 2>&1", tmp_path)
    assert 'bgwatch logs/run.log"' in ctx, ctx
    ctx = hint_for('python train.py > "logs/train.log" 2>&1', tmp_path)
    assert 'bgwatch logs/train.log"' in ctx, ctx


def test_hint_expands_variables_set_in_the_command(tmp_path):
    ctx = hint_for("SD=/var/tmp/push && mkdir -p $SD && python push.py > $SD/push.log 2>&1", tmp_path)
    assert 'bgwatch /var/tmp/push/push.log"' in ctx, ctx
    assert "not `bgwatch babc123`" in ctx, ctx  # the task file is empty when stdout is redirected


def test_hint_says_so_when_a_variable_cannot_be_resolved(tmp_path):
    ctx = hint_for("for i in 1 2; do ./smoke.sh > /var/tmp/smoke$i.log; done", tmp_path)
    assert "can't resolve" in ctx and "/var/tmp/smoke$i.log" in ctx, ctx
    assert 'bgwatch babc123"' not in ctx, ctx


def test_hint_mentions_servers(tmp_path):
    assert "server or tunnel" in hint_for("python train.py", tmp_path)


def state_path(tmp_path, session):
    return tmp_path / f"claude-{os.getuid()}" / "bgwatch_hint" / f"{session}.json"


def test_full_hint_once_per_session_then_one_line(tmp_path):
    first = call_hook(tmp_path, "python a.py", session="s1", task="bfirst001")
    assert "bgwatch wakes you for failure lines" in first and "one-line hint" in first
    second = call_hook(tmp_path, "python b.py > logs/b.log 2>&1", session="s1", task="bsecond02")
    assert second.count("\n") == 0 and len(second) < 250, second
    assert 'Monitor(command="bgwatch logs/b.log"' in second and "the job's own log, not the task file" in second
    assert "bgwatch wakes you" in call_hook(tmp_path, "python c.py", session="s2", task="bthird003")  # a new session


def test_auto_backgrounded_command_waits_for_two_minutes(tmp_path):
    import json
    assert call_hook(tmp_path, "make build", session="s3", task="bauto0003", background=False, timed_out_ms=60000) == ""
    st = json.loads(state_path(tmp_path, "s3").read_text())
    assert [t["task"] for t in st["pending"]] == ["bauto0003"]
    assert call_hook(tmp_path, "ls", session="s3", task=None, background=False) == ""  # still young: nothing yet


def _age_pending(tmp_path, session, task_file, seconds=130):
    import json
    path = state_path(tmp_path, session)
    st = json.loads(path.read_text())
    for t in st["pending"]:
        t["started"] -= seconds
        t["task_file"] = str(task_file)
    path.write_text(json.dumps(st))


def test_deferred_hint_when_still_running(tmp_path):
    call_hook(tmp_path, "make build", session="s4", task="bauto0004", background=False, timed_out_ms=60000)
    out = tmp_path / "bauto0004.output"
    holder = subprocess.Popen(["sleep", "20"], stdout=open(out, "w"))
    try:
        _age_pending(tmp_path, "s4", out)
        ctx = call_hook(tmp_path, "ls", session="s4", task=None, background=False)
        assert "bauto0004" in ctx and re.search(r"still running after \d+ min", ctx), ctx
        assert call_hook(tmp_path, "ls", session="s4", task=None, background=False) == ""  # delivered once
    finally:
        holder.kill()


def test_no_deferred_hint_when_finished_or_already_watched(tmp_path):
    call_hook(tmp_path, "make build", session="s5", task="bauto0005", background=False, timed_out_ms=60000)
    done = tmp_path / "done.output"; done.write_text("ok\n")  # nobody holds it: the job ended
    _age_pending(tmp_path, "s5", done)
    assert call_hook(tmp_path, "ls", session="s5", task=None, background=False) == ""

    call_hook(tmp_path, "make build", session="s6", task="bauto0006", background=False, timed_out_ms=60000)
    out = tmp_path / "bauto0006.output"
    holder = subprocess.Popen(["sleep", "20"], stdout=open(out, "w"))
    watcher = subprocess.Popen(["bash", "-c", "exec -a bgwatch python3 -c 'import time; time.sleep(20)' bauto0006"])
    try:
        time.sleep(0.3)
        _age_pending(tmp_path, "s6", out)
        assert call_hook(tmp_path, "ls", session="s6", task=None, background=False) == ""
    finally:
        holder.kill(); watcher.kill()
