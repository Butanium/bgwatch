#!/usr/bin/env python3
"""Replay bgwatch's failure regex over real job logs, before and after a change.

Every `[fail]` is a wake-up, so a default that fires on echoed model output or test names
costs turns, and one that misses a failure class costs the early warning. Before changing
DEFAULT_FAIL / DEFAULT_IGNORE (or when choosing a --fail for a job), run this against the logs
still on disk and read the difference line by line:

    python3 replay/fail_regex.py --baseline HEAD            # working tree vs the last commit
    python3 replay/fail_regex.py --baseline v1 --files '/var/tmp/*.log' --dump diff.jsonl
    python3 replay/fail_regex.py --fail 'Traceback|Error:' --files train.log   # try a --fail

The corpus is every harness task output file still on disk (`<tmp>/claude-<uid>/*/*/tasks/
*.output`) plus `--files` globs. Output: how many lines each side matches, the distinct lines
only one side matches (with counts), and the files where exactly one side fires at all. A file
where only the baseline fires is where a real failure could go unreported. Labelling each
line as real or false is left to the reader, since a regex can't grade itself.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_defaults(source: str) -> dict:
    """DEFAULT_FAIL / DEFAULT_IGNORE / ANSI_RE from a bgwatch.py source text, without running main()."""
    ns: dict = {"__name__": "bgwatch_replay"}
    exec(compile(source, "bgwatch.py", "exec"), ns)
    return {"fail": ns["DEFAULT_FAIL"], "ignore": ns.get("DEFAULT_IGNORE"), "ansi": ns.get("ANSI_RE")}


def defaults_at(ref: str | None) -> dict:
    if ref is None:
        return load_defaults((REPO / "bgwatch.py").read_text())
    src = subprocess.run(["git", "-C", str(REPO), "show", f"{ref}:bgwatch.py"], capture_output=True, text=True, check=True).stdout
    return load_defaults(src)


def side(fail: str | None, ignore: str | None, ansi, defaults: dict) -> dict:
    return {"fail": re.compile(fail or defaults["fail"]),
            "ignore": re.compile(ignore) if ignore else (re.compile(defaults["ignore"]) if defaults["ignore"] else None),
            "ansi": ansi if ansi is not None else defaults["ansi"]}


def matched(s: dict, lines: list[str]) -> set[str]:
    out = set()
    for line in lines:
        if s["ansi"] is not None:
            line = s["ansi"].sub("", line)
        if s["fail"].search(line) and not (s["ignore"] and s["ignore"].search(line)):
            out.add(line.strip())
    return out


def corpus(patterns: list[str], task_files: bool) -> list[str]:
    paths = set()
    if task_files:
        uid = os.getuid() if hasattr(os, "getuid") else ""
        paths.update(glob.glob(os.path.join(tempfile.gettempdir(), f"claude-{uid}", "*", "*", "tasks", "*.output")))
    for pat in patterns:
        paths.update(glob.glob(os.path.expanduser(pat)))
    return sorted(p for p in paths if os.path.isfile(p) and os.path.getsize(p) > 0 and not _not_a_job_log(p))


def _not_a_job_log(path: str) -> bool:
    """Task files that aren't a job's output: a subagent's transcript (JSONL) or a Monitor's own
    output (bgwatch's lines, which would replay bgwatch against itself)."""
    with open(path, errors="replace") as f:
        head = f.read(64)
    return head.startswith(('{"parentUuid"', "[bgwatch]"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--baseline", metavar="REF", help="git ref whose bgwatch.py defaults are the baseline (default: HEAD)")
    ap.add_argument("--baseline-fail", metavar="RE", help="explicit baseline failure regex instead of a ref's default")
    ap.add_argument("--baseline-ignore", metavar="RE")
    ap.add_argument("--fail", metavar="RE", help="candidate failure regex (default: the working tree's DEFAULT_FAIL)")
    ap.add_argument("--ignore", metavar="RE", help="candidate ignore regex (default: the working tree's DEFAULT_IGNORE)")
    ap.add_argument("--files", nargs="*", default=[], metavar="GLOB", help="more logs to replay over")
    ap.add_argument("--no-task-files", action="store_true", help="skip the harness task output files")
    ap.add_argument("--show", type=int, default=40, metavar="N", help="distinct lines to print per side (default 40)")
    ap.add_argument("--dump", metavar="FILE", help="write every one-sided line as JSONL (file, side, line) for labelling")
    a = ap.parse_args(argv)

    base_defaults = defaults_at(a.baseline or "HEAD") if not a.baseline_fail else {"fail": None, "ignore": None, "ansi": None}
    cand_defaults = defaults_at(None)
    base = side(a.baseline_fail, a.baseline_ignore, None, base_defaults)
    cand = side(a.fail, a.ignore, None, cand_defaults)

    files = corpus(a.files, not a.no_task_files)
    tot = Counter()
    only = {"baseline": Counter(), "candidate": Counter()}
    files_only = {"baseline": [], "candidate": []}
    dump = []
    for path in files:
        lines = open(path, errors="replace").read().splitlines()
        b, c = matched(base, lines), matched(cand, lines)
        tot["files"] += 1
        tot["lines"] += len(lines)
        tot["baseline"] += len(b)
        tot["candidate"] += len(c)
        for name, one in (("baseline", b - c), ("candidate", c - b)):
            for line in one:
                only[name][line[:200]] += 1
                dump.append({"file": path, "side": f"{name}-only", "line": line})
        if b and not c:
            files_only["baseline"].append(path)
        if c and not b:
            files_only["candidate"].append(path)

    print(f"{tot['files']} files, {tot['lines']} lines · distinct matched lines per file, summed: "
          f"baseline {tot['baseline']}, candidate {tot['candidate']}")
    for name in ("baseline", "candidate"):
        print(f"\n== only the {name} matches ({sum(only[name].values())} lines, {len(only[name])} distinct)")
        for line, n in only[name].most_common(a.show):
            print(f"{n:4d}  {line}")
    print(f"\n== files where only the baseline fires ({len(files_only['baseline'])}): read these for missed failures")
    for p in files_only["baseline"]:
        print("  ", p)
    print(f"== files where only the candidate fires ({len(files_only['candidate'])})")
    for p in files_only["candidate"]:
        print("  ", p)
    if a.dump:
        with open(a.dump, "w") as fh:
            for d in dump:
                fh.write(json.dumps(d) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
