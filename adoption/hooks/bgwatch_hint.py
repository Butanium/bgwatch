#!/usr/bin/env python3
"""PostToolUse(Bash) hook: when a command was moved to the background, hand the model
the exact Monitor call that watches it, so arming a watcher is a yes/no instead of
four decisions (pattern, buffering, timeout, exit condition).

Fires only when `tool_response.backgroundTaskId` is present (explicit
run_in_background, or auto-backgrounded at the sync timeout). Subagents are skipped:
they receive neither completion notifications nor Monitor events.

Which file to watch: if the command redirects stdout to a file (`> job.log 2>&1`), that
file — the first whowill A/B (2026-09-14) showed every instance re-pointing the hint at
the job's own log when the hint named the harness task file. Otherwise the harness task
output file, which isn't in the hook payload: it is
<tmp>/claude-<uid>/<cwd slug>/<session_id>/tasks/<task_id>.output, located by glob so a
slug-rule change can't silently break the hint (falls back to the computed path).
"""
import glob
import json
import os
import re
import sys
import tempfile

REDIRECT_RE = re.compile(r"(?<![<>&0-9])(?:&>>?|>>?)\s*(\"?'?)([^\s;&|\"']+)\1")
LEADING_CD_RE = re.compile(r"^\s*cd\s+(\"?'?)([^\s;&|\"']+)\1\s*(?:&&|;)")


def output_file(session_id: str, task_id: str, cwd: str) -> str:
    uid = os.getuid() if hasattr(os, "getuid") else ""
    base = os.path.join(tempfile.gettempdir(), f"claude-{uid}")
    hits = glob.glob(os.path.join(base, "*", session_id, "tasks", f"{task_id}.output"))
    if hits:
        return hits[0]
    slug = cwd.replace("/", "-").replace(".", "-").replace("\\", "-")
    return os.path.join(base, slug, session_id, "tasks", f"{task_id}.output")


def redirect_target(command: str, cwd: str) -> str | None:
    """Last stdout redirect target in the command, resolved against cwd (and a leading
    `cd DIR &&`), or None. `/dev/null` and fd duplications don't count."""
    targets = [m.group(2) for m in REDIRECT_RE.finditer(command) if m.group(2) not in ("/dev/null",)]
    if not targets:
        return None
    target = os.path.expanduser(targets[-1])
    base = cwd
    m = LEADING_CD_RE.match(command)
    if m:
        base = os.path.join(cwd, os.path.expanduser(m.group(2)))
    return os.path.normpath(os.path.join(base, target))


def main() -> None:
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return
    agent_id = data.get("agent_id", "")
    if agent_id and "@" not in agent_id:  # subagent
        return
    resp = data.get("tool_response") or {}
    task_id = resp.get("backgroundTaskId") if isinstance(resp, dict) else None
    if not task_id:
        return
    tool_input = data.get("tool_input") or {}
    command = (tool_input.get("command") or "").lstrip()
    if command.startswith("sleep ") or command.startswith("bgwatch"):
        return  # a timer or a watcher is not a job to watch
    desc = (tool_input.get("description") or "background job").replace('"', "'")
    cwd = data.get("cwd", "")
    task_file = output_file(data.get("session_id", ""), task_id, cwd)
    log = redirect_target(command, cwd)
    if log:
        rel = os.path.relpath(log, cwd)
        watch = rel if not rel.startswith("..") else log
        alt = f" (or the harness task file: bgwatch {task_id})"
    else:
        watch = task_id  # bgwatch resolves a bare task id to its output file
        alt = ""
    hint = (
        f"Background task {task_id} launched. If it runs longer than a couple of minutes, arm its watcher now "
        f"(one call; then keep working or end the turn — do not poll):\n"
        f'  Monitor(command="bgwatch {watch}", persistent=true, description="{desc}")\n'
        f"bgwatch wakes you for failure lines, a heartbeat that backs off from 1 to 10 min, silence longer than "
        f"the job's own output cadence, and the job's exit (detected because the job holds that file open — "
        f"no --pid/--pgrep needed when the job writes the watched file{alt}); then it exits itself. "
        f"Add --match RE for a progress marker, --ignore RE / --fail-also RE to tune patterns (`bgwatch --help`). "
        f"Not needed for a job that ends in seconds: the completion notification covers it. "
        f"Monitor is a deferred tool — `ToolSearch select:Monitor` first if it isn't loaded."
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": hint}}))


if __name__ == "__main__":
    main()
