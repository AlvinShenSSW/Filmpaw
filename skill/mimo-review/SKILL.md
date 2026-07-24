---
name: mimo-review
description: >-
  Run MiMo Code CLI as an optional, read-only external structural review gate on
  the current PR/branch using mimo/mimo-v2.5-pro, then triage and fix findings.
  Use when the operator asks for MiMo review, "mimo gate", or wants MiMo to
  participate in AFK. MiMo is additive/backup: it may run after the outer gate
  and before Kimi, but it never replaces Kimi as the final review.
---

# MiMo Review Gate

Run the **MiMo review gate** as an optional independent reviewer powered by
MiMo Code CLI and `mimo/mimo-v2.5-pro`. MiMo is an additive AFK reviewer: use it
as a backup or extra structural review stage, but keep **Kimi as the final gate**.

The helper ships with this repo at
[`skill/mimo-review/mimo-gate.mjs`](mimo-gate.mjs). It creates a temporary
read-only reviewer config for MiMo, asks it to review the git diff, and prints a
single marker block.

## AFK Position

Default order when MiMo is enabled:

`CTO/self-review -> outer gate -> MiMo optional add-on -> Kimi final`

Rules:

- Never use MiMo to review its own implementation if MiMo becomes the implementer.
- Never replace Kimi with MiMo in the standing AFK workflow; Kimi remains last.
- If MiMo is unavailable, skip it cleanly and report the skip. This is not a
  degraded two-gate run by itself unless the required outer gate or Kimi also
  skipped.
- Keep MiMo calls low-frequency: batch fixes, self-review, then re-run once if
  structural findings were confirmed.

## How To Run

Capture stdout/stderr and pass target flags through:

Windows PowerShell:

```powershell
node "skill/mimo-review/mimo-gate.mjs" --base master 1> "$env:TEMP\mimo_gate.out" 2> "$env:TEMP\mimo_gate.err"
```

macOS/Linux:

```bash
node "skill/mimo-review/mimo-gate.mjs" --base master 1> "${TMPDIR:-/tmp}/mimo_gate.out" 2> "${TMPDIR:-/tmp}/mimo_gate.err"
```

Supported target flags:

- `--base <branch>`: review current branch vs base.
- `--commit <sha>`: review one commit.
- `--uncommitted`: review staged, unstaged, and untracked changes.
- `--project-audit`: review the whole repository for architecture, security,
  dependency, desktop/web parity, release, tests, and workflow risks.

Read the result between:

```text
===== MIMO REVIEW (final message) =====
...
===== END MIMO REVIEW =====
```

If the result starts with `SKIPPED:`, report the reason and continue.

## Triage

Handle findings like the Codex/Kimi gates:

1. Confirm each structural finding by reading the cited code.
2. Fix confirmed P1/P2 issues in one batch.
3. Reject false positives with concrete file/line evidence.
4. Run affected checks and one self-review pass.
5. Re-run MiMo at most once per fix batch unless the operator asks for more.

Stop when MiMo returns no new P1 blockers, or only minor/implementation-detail
comments remain after 2-3 rounds.

## Setup

Install:

```powershell
npm install -g @mimo-ai/cli
```

Authenticate:

```powershell
mimo providers login
mimo providers list
mimo models mimo
```

The gate uses `mimo/mimo-v2.5-pro` by default. If `mimo models mimo` only shows
`mimo/mimo-auto`, finish login/token-plan setup or explicitly override the model
for a degraded local run. Do not silently downgrade the AFK gate.

```powershell
$env:MIMO_REVIEW_MODEL = "mimo/mimo-v2.5-pro"
```

Disable locally:

```powershell
$env:MIMO_REVIEW_GATE = "off"
```
