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

def output_file(session_id: str, task_id: str, cwd: str) -> str:
    uid = os.getuid() if hasattr(os, "getuid") else ""
    base = os.path.join(tempfile.gettempdir(), f"claude-{uid}")
    hits = glob.glob(os.path.join(base, "*", session_id, "tasks", f"{task_id}.output"))
    if hits:
        return hits[0]
    slug = cwd.replace("/", "-").replace(".", "-").replace("\\", "-")
    return os.path.join(base, slug, session_id, "tasks", f"{task_id}.output")


HEREDOC_RE = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")
QUOTED_RE = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
REDIRECT_RE = re.compile(r"(?<![<>&0-9])(?:&>>?|>>?)\s*([^\s;&|()<>]+)")
ASSIGN_RE = re.compile(r"(?:^|[;&|\s])(?:export\s+)?([A-Za-z_]\w*)=([^\s;&|]+)")
LEADING_CD_RE = re.compile(r"^\s*cd\s+([^\s;&|]+)\s*(?:&&|;)")
# a redirect after one of these writes a file the job reads or a note, not the job's output
WRITER_CMDS = ("cat", "echo", "printf", "tee", "date", "true", ":")


def strip_heredocs(command: str) -> str:
    """Drop heredoc bodies: a `>` inside a script piped to `python - <<'EOF'` is code, not a redirect."""
    lines, out, end = command.split("\n"), [], None
    for line in lines:
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        out.append(line)
        m = HEREDOC_RE.search(line)
        if m:
            end = m.group(3)
    return "\n".join(out)


def redirect_target(command: str, cwd: str) -> tuple[str | None, str | None]:
    """(resolved path, raw text) of the job's stdout redirect, or (None, None) when there is
    none. Heredoc bodies and quoted strings are ignored, and so are redirects of commands that
    write a file for the job (`cat > run.py <<EOF`, `echo … > cfg`). `$VAR`s set earlier in the
    command (`L=/tmp/x; … > $L`) or in the environment are expanded; if one can't be, the path
    comes back None with the raw text, since a watcher needs a path its own shell can open."""
    text = strip_heredocs(command)
    text = re.sub(r"(&?>>?)\s*(['\"])([^'\"\s]*)\2", r"\1 \3", text)  # keep quoted redirect targets
    text = QUOTED_RE.sub("''", text)
    targets = []
    for m in REDIRECT_RE.finditer(text):
        if m.group(1) == "/dev/null" or m.group(1).startswith("&"):
            continue
        simple = re.split(r"&&|\|\||[;|\n]", text[: m.start()])[-1].split()
        if simple and simple[0] in WRITER_CMDS:
            continue
        targets.append(m.group(1))
    if not targets:
        return None, None
    raw = targets[-1]
    env = dict(os.environ)
    for name, value in ASSIGN_RE.findall(text[: text.rfind(raw)]):
        env[name] = re.sub(r"\$\{?(\w+)\}?", lambda v: env.get(v.group(1), v.group(0)), value.strip("'\""))
    target = re.sub(r"\$\{?(\w+)\}?", lambda v: env.get(v.group(1), v.group(0)), raw)
    target = os.path.expanduser(target)
    if "$" in target or "`" in target:
        return None, raw
    base = cwd
    m = LEADING_CD_RE.match(text)
    if m:
        base = os.path.join(cwd, os.path.expanduser(m.group(1)))
    return os.path.normpath(os.path.join(base, target)), raw


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
    log, raw = redirect_target(command, cwd)
    lead = f"Background task {task_id} launched."
    if log:
        rel = os.path.relpath(log, cwd)
        watch = rel if not rel.startswith("..") else log
        alt = f"; not `bgwatch {task_id}`, whose task file only gets what the redirect doesn't catch"
    elif raw:
        watch = f"<absolute path of {raw}>"
        lead += (f" Its stdout goes to `{raw}`, which this hook can't resolve: give bgwatch that file's absolute path, "
                 f"not the task id, whose file stays empty when stdout is redirected.")
        alt = ""
    else:
        watch = task_id  # bgwatch resolves a bare task id to its output file
        alt = ""
    hint = (
        f"{lead} If it runs longer than a couple of minutes, arm its watcher now "
        f"(one call; then keep working or end the turn — do not poll):\n"
        f'  Monitor(command="bgwatch {watch}", persistent=true, description="{desc}")\n'
        f"bgwatch wakes you for failure lines, a heartbeat that backs off from 1 to 10 min, silence longer than "
        f"the job's own output cadence, and the job's exit (detected because the job holds that file open — "
        f"no --pid/--pgrep needed when the job writes the watched file{alt}); then it exits itself. "
        f"Add --match RE for a progress marker, --ignore RE / --fail-also RE to tune patterns (`bgwatch --help`). "
        f"Not needed for a job that ends in seconds (the completion notification covers it), nor for a server or tunnel, which isn't meant to end. "
        f"Monitor is a deferred tool — `ToolSearch select:Monitor` first if it isn't loaded."
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": hint}}))


if __name__ == "__main__":
    main()
