"""Tests for the changes driven by the first whowill A/B (2026-09-14): --pgrep must not
match bgwatch's own wrapper shell, defaults adapt to the job's cadence, the hint hook
names the job's own log when the command redirects to one."""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
BGWATCH = HERE.parent / "bgwatch.py"
FAKE_JOB = HERE / "fake_job.py"
HOOK = HERE.parent / "adoption" / "hooks" / "bgwatch_hint.py"


def start_job(log: Path, *job_args):
    f = open(log, "w")
    proc = subprocess.Popen([sys.executable, str(FAKE_JOB), *job_args], stdout=f, stderr=subprocess.STDOUT)
    f.close()
    return proc


def test_pgrep_ignores_own_ancestors(tmp_path):
    """The harness runs a Monitor command through `bash -c "... bgwatch ... --pgrep PAT"`:
    that wrapper's command line contains PAT and must not count as the job."""
    log = tmp_path / "job.log"
    proc = start_job(log, "--steps", "20", "--dt", "0.3", "--crash-at", "99")  # ~6 s: outlives the first /proc scan
    # watch a file nobody holds, so the fd-holder scan finds nothing and --pgrep is the fallback
    unheld = tmp_path / "unheld.log"; unheld.write_text("x\n")
    wrapper = f'echo wrapper-mentions fake_job.py >/dev/null; exec {sys.executable} {BGWATCH} {unheld} --pgrep fake_job.py --grace 0.5 --every 60 --stall 0 --check-every 0.3'
    p = subprocess.run(["bash", "-c", wrapper], capture_output=True, text=True, timeout=30)
    proc.wait()
    lines = p.stdout.splitlines()
    assert p.returncode == 0, lines
    assert "fallback" in lines[0] and "pgrep" in lines[0], lines
    assert any(l.startswith("[EXIT") for l in lines), lines


def test_pgrep_is_only_a_fallback_when_file_is_held(tmp_path):
    log = tmp_path / "job.log"
    proc = start_job(log, "--steps", "2", "--dt", "0.2", "--crash-at", "99")
    p = subprocess.run([sys.executable, str(BGWATCH), str(log), "--pgrep", "no-such-process-xyz", "--from-start",
                        "--every", "60", "--stall", "0", "--check-every", "0.3"], capture_output=True, text=True, timeout=30)
    proc.wait()
    lines = p.stdout.splitlines()
    assert "job-end detection: pid" in lines[0], lines  # fd-holder won; the pattern was never consulted
    assert any(l.startswith("[EXIT") for l in lines), lines


def test_heartbeat_reports_idle_time(tmp_path):
    log = tmp_path / "job.log"
    proc = start_job(log, "--steps", "1", "--dt", "3", "--crash-at", "99")
    p = subprocess.run([sys.executable, str(BGWATCH), str(log), "--from-start", "--every", "1", "--stall", "0",
                        "--check-every", "0.3"], capture_output=True, text=True, timeout=30)
    proc.wait()
    hb = [l for l in p.stdout.splitlines() if l.startswith("[hb ")]
    assert hb and all(re.search(r"idle \d+:\d\d", l) for l in hb), p.stdout


def test_auto_stall_adapts_to_cadence(tmp_path):
    """Job prints every 0.2 s then goes silent for 2.5 s: with --stall auto the threshold is
    clamp(5 * max_gap, --stall-min, --stall-max); with --stall-min 1 that is ~1 s, so the
    silence is reported. A fixed --stall 30 would stay quiet."""
    log = tmp_path / "job.log"
    proc = start_job(log, "--steps", "4", "--dt", "0.2", "--crash-at", "2", "--stall", "2.5")
    p = subprocess.run([sys.executable, str(BGWATCH), str(log), "--from-start", "--every", "60", "--stall", "auto",
                        "--stall-min", "1", "--check-every", "0.3", "--poll", "0.1"], capture_output=True, text=True, timeout=40)
    proc.wait()
    lines = p.stdout.splitlines()
    assert any(l.startswith("[STALL ") for l in lines), lines
    assert any(l.startswith("[resumed ") for l in lines), lines


def test_auto_heartbeat_backs_off(tmp_path):
    log = tmp_path / "job.log"
    proc = start_job(log, "--steps", "1", "--dt", "7", "--crash-at", "99")
    p = subprocess.run([sys.executable, str(BGWATCH), str(log), "--from-start", "--every", "auto", "--every-min", "1",
                        "--every-max", "4", "--stall", "0", "--check-every", "0.3"], capture_output=True, text=True, timeout=40)
    proc.wait()
    hb = [float(re.match(r"\[hb (\d+):(\d+)\]", l).group(1)) * 60 + float(re.match(r"\[hb (\d+):(\d+)\]", l).group(2))
          for l in p.stdout.splitlines() if l.startswith("[hb ")]
    # 1, 2 (=1+1*2), 4 (=2+2), then capped at 4: 8 … ; job lasts ~7 s so expect ~1, 3, 7
    assert len(hb) >= 2, p.stdout
    gaps = [b - a for a, b in zip(hb, hb[1:])]
    assert gaps[0] >= 1.5 * 1 - 0.5, (hb, gaps)  # second interval longer than the first
    assert all(g <= 4.5 for g in gaps), (hb, gaps)


def test_hint_names_redirect_target(tmp_path):
    payload = {
        "session_id": "sess-1", "cwd": str(tmp_path), "tool_name": "Bash",
        "tool_input": {"command": "cd sub && python train.py --epochs 3 > logs/train.log 2>&1", "description": "Train", "run_in_background": True},
        "tool_response": {"stdout": "", "stderr": "", "backgroundTaskId": "babc123"},
    }
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True)
    ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
    assert 'bgwatch sub/logs/train.log"' in ctx, ctx  # cwd-relative: fewer tokens to retype
    assert "bgwatch babc123" in ctx  # the harness file is still offered, by id
    assert "no --pid/--pgrep needed" in ctx  # default detection is explained, --pgrep is not suggested
    # no redirect → the harness task output file is the target
    payload["tool_input"]["command"] = "python train.py 2>&1"
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True)
    ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
    assert 'bgwatch babc123"' in ctx, ctx


def test_bare_task_id_resolves(tmp_path, monkeypatch):
    import os, tempfile
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    tempfile.tempdir = None
    uid = os.getuid()
    d = tmp_path / f"claude-{uid}" / "-some-slug" / "sess-9" / "tasks"; d.mkdir(parents=True)
    log = d / "bq7abc123.output"
    proc = start_job(log, "--steps", "2", "--dt", "0.2", "--crash-at", "99")
    p = subprocess.run([sys.executable, str(BGWATCH), "bq7abc123", "--from-start", "--every", "60", "--stall", "0",
                        "--check-every", "0.3"], capture_output=True, text=True, timeout=30, env={**os.environ, "TMPDIR": str(tmp_path)})
    proc.wait()
    lines = p.stdout.splitlines()
    assert p.returncode == 0 and str(log) in lines[0], lines
    assert any(l.startswith("[EXIT") for l in lines), lines
