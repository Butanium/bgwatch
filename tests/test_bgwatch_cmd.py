"""--cmd: a rerun command as the source, for jobs with no growing file (dump-and-exit CLIs,
batch / queue status)."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
BGWATCH = HERE.parent / "bgwatch.py"
sys.path.insert(0, str(HERE.parent))
from bgwatch import WINDOWS, changed_lines, win_cmdline, win_processes  # noqa: E402


def running(pattern: str) -> bool:
    """`pgrep -A -f pattern` found something (Windows: a command-line scan)."""
    if WINDOWS:
        return any(pattern in (win_cmdline(p) or "") for p in win_processes())
    return subprocess.run(["pgrep", "-A", "-f", pattern], capture_output=True).returncode == 0


def status_script(tmp_path: Path, fail_at: int) -> Path:
    """Each run prints a fixed header and one more 'event' line; run `fail_at` exits 2."""
    counter = tmp_path / "n"
    counter.write_text("0")
    script = tmp_path / "status.sh"
    script.write_text(
        "#!/bin/bash\n"
        f"n=$(cat {counter.as_posix()}); n=$((n+1)); echo $n > {counter.as_posix()}\n"
        "echo 'header: always the same'\n"
        "for i in $(seq 1 $n); do echo \"event $i\"; done\n"
        f"[ $n -eq {fail_at} ] && {{ echo boom >&2; exit 2; }}\n"
        "exit 0\n")
    script.chmod(0o755)
    return script


def test_changed_lines_rolling_window_and_status():
    assert changed_lines(["a", "b", "c"], ["b", "c", "d", "e"]) == ["d", "e"]
    assert changed_lines(["state: running", "n=3"], ["state: done", "n=3"]) == ["state: done"]
    assert changed_lines(["x", "x"], ["x", "x", "x"]) == ["x"]


def test_cmd_emits_changes_fails_on_error_and_exits_on_until(tmp_path):
    script = status_script(tmp_path, fail_at=3)
    until = f"[ $(cat {(tmp_path / 'n').as_posix()}) -ge 5 ]"
    p = subprocess.run([sys.executable, str(BGWATCH), "--cmd", script.as_posix(), "--cmd-every", "0.5",
                        "--until", until, "--every", "0"], capture_output=True, text=True, timeout=60)
    out = p.stdout.splitlines()
    assert p.returncode == 0
    assert "[match] [bgwatch --cmd] baseline: 2 lines; printing what changes" in out
    assert not any(l == "[match] event 1" or "header" in l for l in out)  # baseline is not replayed
    assert [l for l in out if l.startswith("[fail]")] == ["[fail] [bgwatch --cmd] the command exited with code 2: boom"]
    # the failed run 3 is skipped; run 4 is diffed against run 2, so event 3 still arrives once
    assert [l for l in out if l.startswith("[match] event")] == [f"[match] event {i}" for i in (2, 3, 4, 5)]
    assert any(l.startswith("[EXIT ") for l in out)


def test_cmd_match_narrows_and_from_start_prints_baseline(tmp_path):
    script = status_script(tmp_path, fail_at=99)
    until = f"[ $(cat {(tmp_path / 'n').as_posix()}) -ge 3 ]"
    p = subprocess.run([sys.executable, str(BGWATCH), "--cmd", script.as_posix(), "--cmd-every", "0.5",
                        "--until", until, "--every", "0", "--match", "event [13]", "--from-start"],
                       capture_output=True, text=True, timeout=60)
    out = p.stdout.splitlines()
    assert [l for l in out if l.startswith("[match]")] == ["[match] event 1", "[match] event 3"]


def test_cmd_poller_dies_with_its_watcher(tmp_path):
    script = status_script(tmp_path, fail_at=99)
    w = subprocess.Popen([sys.executable, str(BGWATCH), "--cmd", script.as_posix(), "--cmd-every", "0.5", "--every", "0"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    watcher = w.pid
    if WINDOWS:  # a venv's python.exe is a launcher: the watcher is its child, and survives w.kill()
        watcher = next((p for p, pp in win_processes().items() if pp == w.pid), w.pid)
    pattern = f"_cmdpoll --parent {watcher} "
    assert running(pattern)
    if watcher != w.pid:
        os.kill(watcher, signal.SIGTERM)
    w.kill()
    w.wait()
    time.sleep(2.5)
    assert not running(pattern)


def test_cmd_refuses_a_file_and_until_needs_cmd(tmp_path):
    p = subprocess.run([sys.executable, str(BGWATCH), str(tmp_path / "x.log"), "--cmd", "true"], capture_output=True, text=True)
    assert p.returncode == 2 and "--cmd is the source" in p.stderr
    p = subprocess.run([sys.executable, str(BGWATCH), str(tmp_path / "x.log"), "--until", "true"], capture_output=True, text=True)
    assert p.returncode == 2 and "--until needs --cmd" in p.stderr
