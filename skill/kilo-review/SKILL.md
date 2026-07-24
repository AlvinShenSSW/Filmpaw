---
name: kilo-review
description: >
  Run Kilo Code CLI with GLM 5.2 as a read-only external review gate. Use when
  the operator asks for Kilo review, GLM 5.2 review, Kilo Code review, Kilo
  final review, or an experimental GLM 5.2 final gate. Defaults to
  z-ai/glm-5.2 and should normally run as an optional add-on before Kimi unless
  the operator explicitly chooses Kilo as the final reviewer.
---

# Kilo Review Gate

Run the in-repo helper:
[`skill/kilo-review/kilo-gate.mjs`](kilo-gate.mjs). It drives Kilo Code in
read-only review mode using **GLM 5.2** by default:

- model: `z-ai/glm-5.2`
- variant: `max`
- output markers: `===== KILO REVIEW (final message) =====`

## AFK Position

Default: Kilo is an **optional add-on or backup** after the 外门 and before Kimi.
It does not silently replace Kimi.

Experimental final gate: if the operator explicitly says "Kilo 终审",
"GLM 5.2 终审", or equivalent, Kilo may be the final reviewer. Record that choice
in the PR notes and final report. The reviewer must still be different from the
implementer.

Keep Kimi as the default final reviewer for product-wide/cross-route correctness
unless the operator explicitly chooses Kilo final. Treat GLM 5.2 as especially
useful for local logic, security, auth, config, and edge-case review.

## How To Run

Capture stdout and stderr because Kilo reviews can be slow:

- Windows:
  `node "skill/kilo-review/kilo-gate.mjs" --base master 1> "$env:TEMP\kilo_gate.out" 2> "$env:TEMP\kilo_gate.err"`
- macOS/Linux:
  `node "skill/kilo-review/kilo-gate.mjs" --base master 1> "${TMPDIR:-/tmp}/kilo_gate.out" 2> "${TMPDIR:-/tmp}/kilo_gate.err"`

Target flags:

- `--base <branch>`: review current branch against a base branch.
- `--commit <sha>`: review one commit.
- `--uncommitted`: review local uncommitted changes.
- `--pr <number-or-url>`: review a pull request target supported by Kilo.

If the output says `SKIPPED: ...`, report the skip and continue according to the
AFK degraded-review rules. Do not retry in a loop.

## Triage Discipline

Use the same discipline as `kimi-review` and `codex-review`:

1. Treat Kilo findings as hypotheses; verify cited files and lines.
2. Fix confirmed structural findings in one batch.
3. Reject false positives with evidence.
4. Run one self-review pass after fixes.
5. Re-run the gate only when structural fixes justify the cost.
6. Defer nitpicks and doc-only suggestions to a single final pass.

## Setup

Install and authenticate once per machine:

```bash
npm install -g @kilocode/cli
kilo --version
kilo auth login
```

If interactive auth is preferred, run `kilo` and use `/connect`.

Environment overrides:

- `KILO_REVIEW_MODEL`: default `z-ai/glm-5.2`
- `KILO_REVIEW_VARIANT`: default `max`
- `KILO_REVIEW_GATE=off`: force a clean skip
