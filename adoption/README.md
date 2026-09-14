# bgwatch adoption package

What a config needs so instances actually reach for bgwatch. The A/B in whowill applies
this to a sandbox clone of ~/.claude; nothing here is applied to the real config until
the A/B says it works and Clément approves the CLAUDE.md change.

- `hooks/bgwatch_hint.py` — PostToolUse(Bash) hook. On a background launch it injects
  the exact `Monitor(command="bgwatch <output-file>", persistent=true, ...)` call for
  that task. Copy into `<config>/hooks/`.
- `settings_hooks_fragment.json` — the hooks entry to merge into `<config>/settings.json`.
- `CLAUDE_md_snippet.md` — replaces the "Watching progress" bullets of the Bash execution
  section in CLAUDE.md (or CLAUDE.template.md, which regenerates it at SessionStart).
- `bgwatch` on PATH: `~/.local/bin/bgwatch -> ../../.claude/tools/bgwatch/bgwatch.py`.

`READY` is touched when all of the above exist; the fork polls for it.
