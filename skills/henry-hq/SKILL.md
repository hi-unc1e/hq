---
name: henry-hq
description: Use the henry-hq project protocol to inspect STATUS, answer pending decisions, run acceptance checks, and report evidence before an agent ends work.
---

# henry-hq

Use this skill when a project contains `STATUS.md`, `ACCEPTANCE.md`, and
`DECISIONS.md` managed by henry-hq.

## Start and finish

1. Read the project's `STATUS.md`, `DECISIONS.md`, and `ACCEPTANCE.md`.
2. Make the requested change and run the focused checks.
3. Run `hq verify --tier quick` (or the project's documented full checks).
4. Update `STATUS.md` with the result, machine-verifiable evidence, blockers,
   and next steps. Do not hand-edit the generated machine section.

Useful commands:

```text
hq todo                 # pending decisions for the current project
hq ok <project>#<n>     # approve a pending item
hq note <project>#<n> "comment"
hq verify --tier quick
hq status
```

The Stop hook uses `hq gate` and reads JSON from stdin. It must fail open if
the checker itself is unavailable.

## Installation

This repository follows the Agent Skills layout, so ZCode users can install
the skill with the standard skills CLI:

```text
npx skills add hi-unc1e/hq --skill henry-hq --agent zcode -g -y
```

For other supported agents, replace `zcode` with the agent name or omit
`--agent` to choose interactively. Install the `hq` command separately by
cloning this repository and putting `bin` on `PATH`; on Windows use
`bin\\hq.cmd` (PowerShell can call `bin\\hq.ps1`).

## Windows

The Python CLI works on Windows. Use Python 3.11+, keep the project paths in
`hq.toml` as Windows paths, and write acceptance checks using commands
available on that machine. `hq verify` selects `cmd.exe` on Windows and a
POSIX shell on macOS/Linux.
