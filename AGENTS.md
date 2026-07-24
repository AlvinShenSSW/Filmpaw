# AGENTS.md — Filmpaw skill collection

This repo is a collection of **Claude Code skills** that together implement an
autonomous, review-gated software-delivery pipeline (**AFK mode**) plus supporting
design/planning skills. This file is the map: who the agents are, how they hand off,
and what each needs to run.

> Skills live under [`skill/`](skill/). To make them invocable as `/afk`,
> `/codex-review`, etc., they must be registered into a `.claude/skills/` directory
> (see **Registration** below). The `skill/` folder in this repo is the **source of
> truth**; installed copies are derived from it.

## Agent roster

| Skill | Role | Phase | Drives |
|---|---|---|---|
| [`afk`](skill/afk/SKILL.md) | Orchestrator — autonomous waterfall executor | all | Claude (self-scheduled cron relay) |
| [`spec-planner`](skill/spec-planner/SKILL.md) | Tech-lead planner: issue → reviewable plan (no code) | planning | Claude |
| [`implementation-pilot`](skill/implementation-pilot/SKILL.md) | Developer: executes an approved plan, self-review loop | implementation | Claude (Sonnet) |
| [`cto-pr-review`](skill/cto-pr-review/SKILL.md) | CTO gate: APPROVE / APPROVE-WITH-COMMENTS / BLOCK | pre-merge | Claude (implementer's own gate) |
| [`codex-review`](skill/codex-review/SKILL.md) | External gate — OpenAI Codex CLI | pre-merge | `codex exec review` |
| [`kimi-review`](skill/kimi-review/SKILL.md) | External gate — Kimi Code CLI | pre-merge | `kimi -p` |
| [`kilo-review`](skill/kilo-review/SKILL.md) | External gate — Kilo Code CLI / **GLM 5.2** (`z-ai/glm-5.2`) | pre-merge | `kilo run` |
| [`mimo-review`](skill/mimo-review/SKILL.md) | External gate — MiMo CLI (`mimo/mimo-v2.5-pro`) | pre-merge | `mimo` |
| [`ui-ux-pro-max`](skill/ui-ux-pro-max/SKILL.md) | Standalone UI/UX design intelligence (not part of AFK) | design | Python search DB |

## The AFK waterfall (per issue, one at a time)

```
design doc → adversarial debate → TDD (RED→GREEN) → tests/lint → constitution gate
  → commit → push early → open PR → watch CI → CTO self-review
  → external review (外门 → 终审) → merge per policy
```

### Reviewer topology — the invariant

**No model ever reviews its own implementation.** Every external reviewer must be a
*different* model from the implementer.

- **`/afk` (default, Claude-driven):** Claude implements + CTO-reviews. External:
  **Codex = 外门 (outer gate, runs first)** → **Kimi = 终审 (final, runs last)**.
- **`/afk codex` (Codex-driven):** Codex implements + CTO-reviews. External:
  **Claude = 外门** → **Kimi = 终审**. *Never* Codex as a reviewer here.

**Optional add-ons** (between 外门 and 终审): **MiMo** and **Kilo/GLM 5.2**. Kilo may
become the experimental 终审 **only** when the operator explicitly says so
("Kilo 终审" / "GLM 5.2 终审").

**Degraded review:** if one required external reviewer is unavailable (out of quota /
not installed / not logged in), run the remaining one and **flag the degraded run** to
the operator inline and in the end-of-run report. Only if **both** are unavailable,
fall back to CTO review alone and record `external gate unavailable`. Never silently
drop a reviewer.

### Continuity & self-pause

AFK creates a recurring **~30-min cron** that re-invokes itself each tick (survives
pauses / rate-limits / context resets), tracks substantial new content per tick, and
**auto-pauses after 2 consecutive idle ticks** (deletes the scheduler, posts a status
report, stops). It **never** deploys — merge ≠ deploy; the operator pulls and restarts.

### Kickoff contract

AFK **requires an operator-provided scope** (explicit issues/PRs/file areas) — it
never picks work from the tracker itself. Confirm merge policy (`leave-open` default /
`merge-to-unblock` / `merge-when-green`) and constraints, restate the scope in one
line, then start.

## Registration

These are installed as **personal skills** so `/afk` and the gates are available in
every project (AFK operates on a *target* repo, not on this collection):

```
~/.claude/skills/<name>/     →  Windows: C:\Users\<you>\.claude\skills\<name>\
```

Re-sync after editing the source here by copying `skill/*` into `~/.claude/skills/`.
Newly installed skills are picked up when Claude Code starts a fresh session.

> **Gate-script paths.** The SKILL.md docs show `node "skill/<name>/<gate>.mjs"` —
> that relative path assumes cwd is *this* repo. When a gate runs during AFK on
> another project, invoke the installed copy by absolute path, e.g.
> `node "C:\Users\<you>\.claude\skills\codex-review\codex-gate.mjs"`. The gate reads
> the **current directory's** git diff, so run it from the target repo.

## CLI invocation discipline (anti-hang rules — MUST follow)

Driving Codex/Kimi/Kilo/MiMo headless has bitten us with hangs. Rules:

1. **Always close stdin**: append `< /dev/null` (bash) or pipe empty input. `codex exec`
   without it blocks forever on "Reading additional input from stdin...".
2. **Always set a hard timeout** on the invocation (tool timeout or the gate script's
   built-in `*_TIMEOUT_MS`). Never launch an external CLI with no upper bound.
3. **Prefer the in-repo gate scripts** (`skill/*-review/*-gate.mjs`) over raw CLI calls —
   they already handle per-OS flags, timeout caps, skip-detection, and marker output.
4. **Never use interactive flags headless**: `mimo --prompt` opens the TUI (use
   `mimo run ... --format` via mimo-gate.mjs); no `-i`/REPL modes; nothing that can prompt.
5. **Run long calls in background + notification**, never poll-sleep; if stderr shows a
   "waiting/reading input" line and stdout stays empty for minutes, kill and re-invoke
   with stdin closed instead of waiting.

## External CLI dependencies (per machine, once)

All gates are **optional and self-skipping** — a missing/unauthenticated CLI emits a
`SKIPPED: …` marker instead of failing. Disable any gate permanently with its
`*_REVIEW_GATE=off` env var (`CODEX_REVIEW_GATE`, `KIMI_REVIEW_GATE`,
`KILO_REVIEW_GATE`, `MIMO_REVIEW_GATE`).

| Tool | Install | Auth |
|---|---|---|
| Node + git | required by every gate | — |
| `gh` | GitHub CLI (issues/PRs/CI) | `gh auth login` |
| Codex | `npm i -g @openai/codex` | `codex login` (ChatGPT sub, no API key) |
| Kimi | `npm i -g @moonshot-ai/kimi-code` | `kimi login` (device-code / API key) |
| Kilo (GLM 5.2) | `npm i -g @kilocode/cli` | `kilo auth login` |
| MiMo | MiMo Code CLI | per MiMo docs |
| `uv` + Python | project tests (`uv run --locked … pytest`) | — |

### Secrets — `.env`

Real API keys go in a local **`.env`** (gitignored — never committed to this public
repo). Copy the template and fill in what you use:

```bash
cp .env.example .env      # PowerShell: Copy-Item .env.example .env
```

Most gates authenticate via their CLI's own `login` (see the table above) and need
**no** key in `.env`. The main env-key case is **GLM 5.2 / Kilo** if you drive it via
an API key instead of `kilo auth login` — put it in `ZAI_API_KEY`. `.env` also holds
optional gate-tuning vars (model overrides, `*_REVIEW_GATE=off`). Load it into your
shell before an AFK run that depends on those vars, e.g.:

```bash
# PowerShell
Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
  $k,$v = $_ -split '=',2; if ($v) { [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim()) }
}
```

### Status on this machine (checked 2026-07-24)

- ✅ Installed & on PATH: `node`, `git`, `gh`, `codex`, `kimi`, `mimo`, `kilo`
  (v7.4.15, GLM 5.2 gate), `python`, `uv`
- Auth status of each CLI is not verified here — confirm `codex login` / `kimi login`
  / `kilo auth login` before an AFK run that relies on them.
