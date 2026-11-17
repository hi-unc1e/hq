---
name: henry-hq
description: Use the henry-hq project protocol to inspect STATUS, answer pending decisions, run acceptance checks, diagnose/tune harness guardrails, and report evidence before an agent ends work.
---

# henry-hq

For timeout decisions, install the local hooks with
`python3 bin/hq.py install-auto-default` from the hq repository (Windows:
`python bin/hq.py install-auto-default`). Supports Codex, Qoder and ZCode.
Use `--check` to verify installation. Only explicit A/B/C questions with one
option marked `最推荐` qualify; never default risky actions or approvals.
After five minutes the hook asks the agent to continue. A user reply cancels.
The client must actually dispatch the hooks; skill installation alone does
not install hooks or prove runtime behavior.

Use this skill when a project contains `STATUS.md`, `ACCEPTANCE.md`, and
`DECISIONS.md` managed by henry-hq.

## Start and finish

1. Read `STATUS.md`, `DECISIONS.md`, and `ACCEPTANCE.md`.
2. **Memory synthesis, not CRUD hoarding**:
   - Keep `STATUS.md` human content under one screen (`<= 75` lines before `<!-- hq:verify:start -->`).
   - Keep the top `> ...` summary as plain text (`<= 60` chars, no raw URLs or redundant prefixes) so `hq status` displays cleanly without truncating mid-URL.
   - Prune resolved `[x]` items from `❓` after folding lasting takeaways into `DECISIONS.md` domain sections; archive raw history under `.hq/history/`.
3. **Prioritize the 20% core**: Deliver working core flows and focused checks first (`just preflight`, `hq verify --tier quick`), or 80/20 surgical UI verification clips rather than bloating context or logs.
4. Before ending a turn, run `hq verify --tier quick` and update `STATUS.md` (result, blockers, next steps, and only human-only decisions in `❓`). Never hand-edit the `✅` machine section.

Useful commands:

```text
hq todo                 # pending decisions for the current project
hq ok <project>#<n>     # approve a pending item
hq note <project>#<n> "comment"
hq verify --tier quick  # runs ACCEPTANCE.md checks (ideally wired to `just` / project lint+test)
hq status
hq diagnose             # audit harness guardrails, doc bloat, and Codex/Claude/DSH telemetry
hq tune --apply         # install cross-harness (Codex/Claude) 4-stage hooks + single hq gate
```

The Stop hook uses `hq gate` and reads JSON from stdin. It fails open if
the checker itself is unavailable and runs alongside fast (`<200ms`) static
invariant hooks without duplicating `hq gate`.

## Installation

```text
npx skills add hi-unc1e/hq --skill henry-hq --agent zcode -g -y
```

Install the `hq` command separately by cloning this repository and putting `bin`
on `PATH`; on Windows use `bin\\hq.cmd` (PowerShell can call `bin\\hq.ps1`).

## Windows

The Python CLI works on Windows. Use Python 3.11+, keep project paths in
`hq.toml` as Windows paths, and write acceptance checks using commands
available on that machine. `hq verify` selects `cmd.exe` on Windows and a
POSIX shell on macOS/Linux.
