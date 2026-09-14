#!/usr/bin/env python3
"""PostToolUse(Bash) hook: when a command was moved to the background, hand the model
the exact Monitor call that watches it, so arming a watcher is a yes/no instead of
four decisions (pattern, buffering, timeout, exit condition).

Fires only when `tool_response.backgroundTaskId` is present (explicit
run_in_background, or auto-backgrounded at the sync timeout). Subagents are skipped:
they receive neither completion notifications nor Monitor events.

The task's output file path isn't in the hook payload; it is
<tmp>/claude-<uid>/<cwd slug>/<session_id>/tasks/<task_id>.output, located by glob so a
slug-rule change can't silently break the hint (falls back to the computed path).
"""
import glob
import json
import os
import sys
import tempfile


def output_file(session_id: str, task_id: str, cwd: str) -> str:
    uid = os.getuid() if hasattr(os, "getuid") else ""
    base = os.path.join(tempfile.gettempdir(), f"claude-{uid}")
    hits = glob.glob(os.path.join(base, "*", session_id, "tasks", f"{task_id}.output"))
    if hits:
        return hits[0]
    slug = cwd.replace("/", "-").replace(".", "-").replace("\\", "-")
    return os.path.join(base, slug, session_id, "tasks", f"{task_id}.output")


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
    path = output_file(data.get("session_id", ""), task_id, data.get("cwd", ""))
    hint = (
        f"Background task {task_id} launched. If it runs longer than a couple of minutes, arm its watcher now "
        f"(one call; then keep working or end the turn — do not poll):\n"
        f'  Monitor(command="bgwatch {path}", persistent=true, description="{desc}")\n'
        f"bgwatch wakes you for failures, a 10-min heartbeat, stalls and the job's exit, then exits itself. "
        f"Add --match RE for a progress marker, --ignore RE / --fail-also RE to tune patterns "
        f"(`bgwatch --help`). Not needed for a job that ends in seconds: the completion notification "
        f"covers it. Monitor is a deferred tool — `ToolSearch select:Monitor` first if it isn't loaded."
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": hint}}))


if __name__ == "__main__":
    main()
