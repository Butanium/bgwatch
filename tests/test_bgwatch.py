"""bgwatch behaviour tests. Real subprocesses, short intervals; each test < ~10 s."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent
BGWATCH = HERE.parent / "bgwatch.py"
FAKE_JOB = HERE / "fake_job.py"
HOOK = HERE.parent / "adoption" / "hooks" / "bgwatch_hint.py"


def run_watch(args, timeout=40):
    p = subprocess.run([sys.executable, str(BGWATCH), *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.splitlines()


def start_job(log: Path, *job_args):
    """Start fake_job with stdout+stderr redirected to `log` the way the harness does
    (the process holds the file open on fd 1/2)."""
    f = open(log, "w")
    proc = subprocess.Popen([sys.executable, str(FAKE_JOB), *job_args], stdout=f, stderr=subprocess.STDOUT)
    f.close()  # only the child keeps it open
    return proc


def test_traceback_folded_and_exit_detected(tmp_path):
    log = tmp_path / "job.log"
    proc = start_job(log, "--steps", "6", "--dt", "0.3", "--crash-at", "2")
    rc, lines = run_watch([str(log), "--from-start", "--every", "60", "--stall", "0", "--check-every", "0.5"])
    proc.wait()
    assert rc == 0
    fails = [l for l in lines if l.startswith("[fail]")]
    assert fails == ["[fail] Traceback → KeyError: 'labels'", "[fail] ERROR: eval step skipped, continuing"]
    exit_lines = [l for l in lines if l.startswith("[EXIT")]
    assert len(exit_lines) == 1 and "2 fail lines seen" in exit_lines[0]
    assert lines[-1].strip() == "done"  # tail printed after EXIT
    assert not any("error_rate" in l for l in fails)  # word-bounded default


def test_ignore_and_fail_also_and_match(tmp_path):
    log = tmp_path / "job.log"
    proc = start_job(log, "--steps", "4", "--dt", "0.2", "--crash-at", "2")
    rc, lines = run_watch([
        str(log), "--from-start", "--every", "60", "--stall", "0", "--check-every", "0.5",
        "--ignore", "ERROR: eval", "--fail-also", "loss=0\\.9[0-9]", "--match", "^done$",
    ])
    proc.wait()
    assert rc == 0
    fails = [l for l in lines if l.startswith("[fail]")]
    assert "[fail] ERROR: eval step skipped, continuing" not in fails
    assert any("loss=0.9" in l for l in fails)
    assert "[match] done" in lines


def test_no_fail_only_match(tmp_path):
    log = tmp_path / "job.log"
    proc = start_job(log, "--steps", "3", "--dt", "0.2", "--crash-at", "1")
    rc, lines = run_watch([str(log), "--from-start", "--every", "60", "--stall", "0", "--check-every", "0.5", "--no-fail", "--match", "step 3/"])
    proc.wait()
    assert rc == 0
    assert not any(l.startswith("[fail]") for l in lines)
    assert any(l.startswith("[match] step 3/") for l in lines)


def test_stall_and_resume_and_heartbeat(tmp_path):
    log = tmp_path / "job.log"
    proc = start_job(log, "--steps", "3", "--dt", "0.2", "--crash-at", "1", "--stall", "2.5")
    rc, lines = run_watch([str(log), "--from-start", "--every", "1", "--stall", "1", "--check-every", "0.5"])
    proc.wait()
    assert rc == 0
    assert any(l.startswith("[STALL ") and "no output for" in l and "alive" in l for l in lines), lines
    assert any(l.startswith("[resumed ") for l in lines), lines
    assert any(l.startswith("[hb ") and "alive" in l for l in lines), lines


def test_once(tmp_path):
    log = tmp_path / "job.log"
    proc = start_job(log, "--steps", "5", "--dt", "0.5", "--crash-at", "99")
    time.sleep(1)
    rc, lines = run_watch([str(log), "--once", "--grace", "1"])
    assert rc == 0 and len(lines) == 1 and lines[0].startswith("[status ") and "alive" in lines[0]
    proc.wait()
    rc, lines = run_watch([str(log), "--once", "--grace", "0"])
    assert "job state unknown" in lines[0]  # nobody holds the file → no claim


def test_no_holder_keeps_watching_without_exit(tmp_path):
    log = tmp_path / "job.log"
    log.write_text("hello\n")
    # nothing holds the file: bgwatch must say detection is off and not exit on its own
    p = subprocess.Popen([sys.executable, str(BGWATCH), str(log), "--grace", "0.5", "--every", "60", "--stall", "0"],
                         stdout=subprocess.PIPE, text=True)
    first = p.stdout.readline()
    assert "job-end detection: off" in first
    time.sleep(2)
    assert p.poll() is None
    p.kill()


def test_pid_mode(tmp_path):
    log = tmp_path / "job.log"
    proc = start_job(log, "--steps", "3", "--dt", "0.2", "--crash-at", "99")
    rc, lines = run_watch([str(log), "--pid", str(proc.pid), "--from-start", "--every", "60", "--stall", "0", "--check-every", "0.3"])
    assert rc == 0 and any(l.startswith("[EXIT") and f"pid {proc.pid}" in l for l in lines)


def test_rate_limit(tmp_path):
    log = tmp_path / "job.log"
    log.write_text("".join(f"Error {i}\n" for i in range(30)))
    proc = subprocess.Popen(["sleep", "1"], stdout=open(log, "a"))  # a short-lived holder
    rc, lines = run_watch([str(log), "--from-start", "--every", "60", "--stall", "0", "--check-every", "0.3", "--max-rate", "5"])
    proc.wait()
    assert sum(l.startswith("[fail]") for l in lines) == 5
    assert any("suppressed" in l or "fail lines seen" in l for l in lines)


def test_bad_regex_is_an_error():
    p = subprocess.run([sys.executable, str(BGWATCH), "x.log", "--fail", "("], capture_output=True, text=True)
    assert p.returncode == 2 and "bad regex" in p.stderr


def test_hook_hint(tmp_path):
    payload = {
        "session_id": "sess-1", "cwd": str(tmp_path), "tool_name": "Bash",
        "tool_input": {"command": "python train.py", "description": "Train the \"big\" model", "run_in_background": True},
        "tool_response": {"stdout": "", "stderr": "", "backgroundTaskId": "babc123"},
    }
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True)
    out = json.loads(p.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert 'Monitor(command="bgwatch ' in ctx and "babc123.output" in ctx and "persistent=true" in ctx
    assert 'description="Train the \'big\' model"' in ctx
    # no hint for sleep timers, subagents, or non-background calls
    for mutate in (
        lambda d: d["tool_input"].update(command="sleep 60 && echo tick"),
        lambda d: d.update(agent_id="deadbeef"),
        lambda d: d["tool_response"].pop("backgroundTaskId"),
    ):
        d = json.loads(json.dumps(payload)); mutate(d)
        p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(d), capture_output=True, text=True)
        assert p.stdout.strip() == ""
