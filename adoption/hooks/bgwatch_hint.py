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

Linux-only: `held_open` and `already_watched` read /proc. Elsewhere they answer False, so an
auto-backgrounded command never gets its deferred hint; explicit launches are unaffected.
"""
import glob
import json
import os
import re
import sys
import tempfile
import time

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


def monitor_lifetime() -> tuple[str, str]:
    return "persistent=true", ""


def toolsearch_note() -> str:
    return " Monitor is a deferred tool — `ToolSearch select:Monitor` first if it isn't loaded."


DEFER_S = 120  # an auto-backgrounded command gets its hint only if it is still running by then
PENDING_MAX_S = 6 * 3600


def state_file(session_id: str) -> str:
    uid = os.getuid() if hasattr(os, "getuid") else ""
    return os.path.join(tempfile.gettempdir(), f"claude-{uid}", "bgwatch_hint", f"{session_id or 'no-session'}.json")


def load_state(session_id: str) -> dict:
    try:
        with open(state_file(session_id)) as f:
            state = json.load(f)
        if isinstance(state, dict):
            return {"full_shown": bool(state.get("full_shown")), "pending": list(state.get("pending") or [])}
    except (OSError, ValueError):
        pass
    return {"full_shown": False, "pending": []}


def save_state(session_id: str, state: dict) -> None:
    path = state_file(session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def held_open(path: str) -> bool:
    """Some process holds `path` open, i.e. the background task that writes it is still running."""
    target = os.path.realpath(path)
    for fd_dir in glob.glob("/proc/[0-9]*/fd"):
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(os.path.join(fd_dir, fd)) == target:
                    return True
            except OSError:
                continue
    return False


def already_watched(task_id: str, watch: str) -> bool:
    """A bgwatch process already follows this task, by id or by the file it writes."""
    names = {task_id, os.path.basename(watch)}
    for cmdline in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmdline, "rb") as f:
                args = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if any(os.path.basename(a) in ("bgwatch", "bgwatch.py") for a in args[:2]):
            if any(os.path.basename(a) in names for a in args[1:]):
                return True
    return False


def watch_target(command: str, cwd: str, task_id: str) -> tuple[str, str, str | None]:
    """(what to pass bgwatch, kind, raw redirect text); kind is task / redirect / unresolved."""
    log, raw = redirect_target(command, cwd)
    if log:
        rel = os.path.relpath(log, cwd)
        return (rel if not rel.startswith("..") else log), "redirect", raw
    if raw:
        return f"<absolute path of {raw}>", "unresolved", raw
    return task_id, "task", None


def render(t: dict, full: bool, deferred_min: float | None = None) -> str:
    """The hint for one task `t` (keys: task, watch, kind, raw, desc). The first hint of a session
    explains bgwatch; later ones are one line, since the explanation is already in context."""
    task, watch, kind, raw, desc = t["task"], t["watch"], t["kind"], t.get("raw"), t["desc"]
    lifetime, cap_note = monitor_lifetime()
    call = f'Monitor(command="bgwatch {watch}", {lifetime}, description="{desc}")'
    if deferred_min is not None:
        lead = (f"Background task {task} (\"{desc}\"), moved to the background at the sync timeout, is still "
                f"running after {deferred_min:.0f} min.")
    else:
        lead = f"Background task {task} launched."
    unresolved = (f" Its stdout goes to `{raw}`, which this hook can't resolve: give bgwatch that file's absolute "
                  f"path, not the task id, whose file stays empty when stdout is redirected.") if kind == "unresolved" else ""
    if not full:
        redirect = " (the job's own log, not the task file)" if kind == "redirect" else ""
        when = "If you want to hear about failures before it ends" if deferred_min is not None else "If it runs past a couple of minutes"
        return f"{lead}{unresolved} {when}: {call}{redirect}.{cap_note}"
    alt = f"; not `bgwatch {task}`, whose task file only gets what the redirect doesn't catch" if kind == "redirect" else ""
    when = ("Arm its watcher now if you want to hear about failures before it ends"
            if deferred_min is not None else "If it runs longer than a couple of minutes, arm its watcher now")
    return (
        f"{lead}{unresolved} {when} (one call; then keep working or end the turn — do not poll):\n"
        f"  {call}\n"
        f"bgwatch wakes you for failure lines, a heartbeat that backs off from 1 to 10 min, silence longer than "
        f"the job's own output cadence, and the job's exit (detected because the job holds that file open — "
        f"no --pid/--pgrep needed when the job writes the watched file{alt}); then it exits itself. "
        f"Add --match RE for a progress marker, --ignore RE / --fail-also RE to tune patterns (`bgwatch --help`). "
        f"Not needed for a job that ends in seconds (the completion notification covers it), nor for a server or "
        f"tunnel, which isn't meant to end. Later launches in this session get a one-line hint."
        + cap_note + toolsearch_note()
    )


def main() -> None:
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return
    if data.get("agent_id") and "@" not in data["agent_id"]:  # subagent: can't wait on a Monitor
        return
    session = data.get("session_id", "")
    cwd = data.get("cwd", "")
    state = load_state(session)
    changed, out, now = False, [], time.time()

    # Auto-backgrounded commands (the sync timeout moved them) mostly end within a minute or two,
    # so their hint waits until one is still running DEFER_S after its start. Hooks only run on
    # tool calls, so it is delivered with the first Bash call after that point.
    keep = []
    for t in state["pending"]:
        age = now - t["started"]
        if age < DEFER_S:
            keep.append(t)
            continue
        changed = True
        if age < PENDING_MAX_S and held_open(t["task_file"]) and not already_watched(t["task"], t["watch"]):
            out.append(render(t, full=not state["full_shown"], deferred_min=age / 60))
            state["full_shown"] = True
    state["pending"] = keep

    resp = data.get("tool_response") or {}
    task_id = resp.get("backgroundTaskId") if isinstance(resp, dict) else None
    tool_input = data.get("tool_input") or {}
    command = (tool_input.get("command") or "").lstrip()
    if task_id and not (command.startswith("sleep ") or command.startswith("bgwatch")):  # timers and watchers aren't jobs
        desc = (tool_input.get("description") or "background job").replace('"', "'")
        watch, kind, raw = watch_target(command, cwd, task_id)
        t = {"task": task_id, "watch": watch, "kind": kind, "raw": raw, "desc": desc}
        timed_out_ms = resp.get("timedOutAfterMs") if isinstance(resp, dict) else None
        if timed_out_ms or not tool_input.get("run_in_background"):
            t.update(started=now - (timed_out_ms or 0) / 1000,
                     task_file=output_file(session, task_id, cwd))
            state["pending"].append(t)
        else:
            out.append(render(t, full=not state["full_shown"]))
            state["full_shown"] = True
        changed = True

    if changed:
        try:
            save_state(session, state)
        except OSError:
            pass  # a lost state file only means a repeated full hint
    if out:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n\n".join(out)}}))


if __name__ == "__main__":
    main()
