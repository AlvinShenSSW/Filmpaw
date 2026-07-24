#!/usr/bin/env node
// mimo-gate.mjs -- cross-platform MiMo Code external review wrapper.
//
// Runs MiMo Code (`mimo run`) as a READ-ONLY structural review gate using
// mimo/mimo-v2.5-pro and prints only the final review between marker lines.
// MiMo is an optional additive reviewer for AFK: it can run after the outer
// gate and before Kimi, but it never replaces the Kimi final gate.

import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';

const isWin = process.platform === 'win32';
const requestedModel = process.env.MIMO_REVIEW_MODEL || 'mimo/mimo-v2.5-pro';
let model = requestedModel;
let modelNote = '';
const strictModel = ['1', 'true', 'yes', 'on'].includes(
  (process.env.MIMO_REVIEW_STRICT || '').trim().toLowerCase(),
);
// Hard cap so a hung `mimo run` (e.g. waiting on an interactive prompt) can
// never wedge an unattended AFK cron. Floor at 60s; default 10 min.
const timeoutMs = Math.max(
  60_000,
  Number.parseInt(process.env.MIMO_REVIEW_TIMEOUT_MS || '', 10) || 600_000,
);

function emitSkip(reason) {
  process.stderr.write(`[mimo-gate] skipped: ${reason}\n`);
  process.stdout.write('===== MIMO REVIEW (final message) =====\n');
  process.stdout.write(`SKIPPED: ${reason}\n`);
  process.stdout.write('===== END MIMO REVIEW =====\n');
  process.exit(0);
}

const gateFlag = (process.env.MIMO_REVIEW_GATE || '').trim().toLowerCase();
if (['off', '0', 'false', 'no', 'disabled'].includes(gateFlag)) {
  emitSkip('MiMo gate disabled via MIMO_REVIEW_GATE.');
}

function resolveMimo() {
  if (isWin && process.env.APPDATA) {
    const cmdShim = join(process.env.APPDATA, 'npm', 'mimo.cmd');
    if (existsSync(cmdShim)) return cmdShim;
  }
  return 'mimo';
}

function detectBase() {
  const r = spawnSync('git', ['rev-parse', '--abbrev-ref', 'origin/HEAD'], {
    encoding: 'utf8',
  });
  if (r.status === 0 && r.stdout.trim()) return r.stdout.trim().replace(/^origin\//, '');
  for (const b of ['main', 'master']) {
    const v = spawnSync('git', ['rev-parse', '--verify', b], { encoding: 'utf8' });
    if (v.status === 0) return b;
  }
  return 'main';
}

const userArgs = process.argv.slice(2);

function optVal(name) {
  const i = userArgs.indexOf(name);
  return i >= 0 && i + 1 < userArgs.length ? userArgs[i + 1] : null;
}

function sanitizeRef(val, label) {
  if (val === null) return null;
  if (!/^[a-zA-Z0-9._\-/~:@]+$/.test(val)) {
    process.stderr.write(`[mimo-gate] unsafe ${label} value rejected: ${JSON.stringify(val)}\n`);
    process.exit(1);
  }
  return val;
}

const commitArg = sanitizeRef(optVal('--commit'), '--commit');
const uncommitted = userArgs.includes('--uncommitted');
const projectAudit = userArgs.includes('--project-audit');
const baseArg = sanitizeRef(optVal('--base'), '--base');

let scope;
if (projectAudit) {
  scope =
    'the whole repository for a comprehensive project audit. Inspect architecture, security, dependency risk, desktop/web parity, build/release config, agent workflow docs, tests, and high-risk production paths. Use git status and repo search to orient, but do not limit yourself to the current diff';
} else if (commitArg) {
  scope = `the single commit \`${commitArg}\` (inspect with \`git show ${commitArg}\`)`;
} else if (uncommitted) {
  scope = 'all uncommitted changes: staged, unstaged, and untracked (`git diff HEAD`, `git status`, and untracked files)';
} else {
  const base = sanitizeRef(baseArg || detectBase(), '--base');
  scope = `the changes on the current branch versus \`${base}\` (inspect with \`git diff ${base}...HEAD\`)`;
}

const work = mkdtempSync(join(tmpdir(), 'mimo-gate-'));
const promptFile = join(work, 'review-prompt.md');
const logFile = join(work, 'mimo.log');

const reviewPrompt = [
  'You are an independent senior reviewer running an additive external structural gate before Kimi final review.',
  `Review ${scope} in this git repository.`,
  '',
  'Rules:',
  '- READ-ONLY review. Do not modify, stage, commit, write, or delete any file.',
  '- Use git commands and read surrounding files for context.',
  '- Focus on structural issues: architecture/design, correctness bugs, security loopholes, missed edge cases, concurrency/data-integrity, breaking changes.',
  '- Ignore pure nitpicks: naming, formatting, wording, or comments unless they hide a real production risk.',
  '- For each finding output: severity [P1]=blocker / [P2] / [minor], file:line, problem, risk, and a concrete fix.',
  '- Finish with one verdict line: APPROVE / APPROVE WITH COMMENTS / REQUEST CHANGES.',
  '- Output only the review. No preamble.',
].join('\n');

writeFileSync(promptFile, reviewPrompt);

const mimo = resolveMimo();
const version = spawnSync(mimo, ['--version'], { encoding: 'utf8', shell: isWin });
if (version.error && version.error.code === 'ENOENT') {
  emitSkip('MiMo CLI not installed (run: npm install -g @mimo-ai/cli, then `mimo auth login`).');
}
const versionOutput = `${version.stdout || ''}${version.stderr || ''}`;
if (version.status !== 0 && /not recognized|not found|cannot find|ENOENT/i.test(versionOutput)) {
  emitSkip('MiMo CLI not installed (run: npm install -g @mimo-ai/cli, then `mimo auth login`).');
}

// Only run with a model the account can actually select. The default
// mimo/mimo-v2.5-pro is often unavailable (login/plan) and previously crashed
// the gate with ProviderModelNotFoundError instead of skipping or downgrading.
const modelList = spawnSync(mimo, ['models', 'mimo'], {
  encoding: 'utf8',
  shell: isWin,
  env: { ...process.env, MIMOCODE_DISABLE_AUTOUPDATE: 'true' },
  timeout: 60_000,
});
const availableModels = `${modelList.stdout || ''}\n${modelList.stderr || ''}`
  .split(/\r?\n/)
  .map((l) => l.trim())
  .filter((l) => /^mimo\/[\w.-]+$/.test(l));

if (availableModels.length === 0) {
  emitSkip(
    'MiMo has no selectable models for provider `mimo` — run `mimo providers login` and confirm `mimo models mimo` lists a model, or set MIMO_REVIEW_GATE=off.',
  );
}

if (!availableModels.includes(requestedModel)) {
  // Prefer a specific (non-auto) model over mimo/mimo-auto when we must fall back.
  const fallback = availableModels.find((m) => !/auto$/i.test(m)) || availableModels[0];
  process.stderr.write(
    `[mimo-gate] requested model ${requestedModel} unavailable; selectable: ${availableModels.join(', ')}\n`,
  );
  if (strictModel) {
    emitSkip(
      `requested model ${requestedModel} unavailable (selectable: ${availableModels.join(', ')}) — set MIMO_REVIEW_MODEL to one of these, or unset MIMO_REVIEW_STRICT to auto-select.`,
    );
  }
  model = fallback;
  modelNote = `NOTE: requested \`${requestedModel}\` unavailable; ran degraded on selectable model \`${model}\`.`;
  process.stderr.write(`[mimo-gate] downgrading to ${model} (set MIMO_REVIEW_STRICT=1 to skip instead)\n`);
}

const configContent = JSON.stringify({
  $schema: 'https://mimo.xiaomi.com/mimocode/config.json',
  model,
  default_agent: 'mimo-review',
  share: 'disabled',
  autoupdate: false,
  agent: {
    'mimo-review': {
      description: 'Read-only structural PR reviewer for AFK external gates.',
      mode: 'primary',
      model,
      temperature: 0.1,
      tools: {
        write: false,
        edit: false,
        bash: true,
      },
      prompt:
        'You are a read-only code reviewer. Never modify files. Use only read-only git/search commands. Prioritize real correctness, security, reliability, and architecture issues.',
    },
  },
  permission: {
    // Unattended runs have no TTY, so 'ask' auto-rejects — that starves the
    // read-only reviewer of file reads and it stalls until timeout. Allow
    // read-family tools by default; mutations stay denied and bash stays
    // allow-listed below, so this cannot modify the repo.
    '*': 'allow',
    edit: 'deny',
    write: 'deny',
    bash: {
      '*': 'ask',
      'git *': 'allow',
      'rg *': 'allow',
      'grep *': 'allow',
      'Get-Content *': 'allow',
      'Select-String *': 'allow',
      'ls *': 'allow',
      'dir *': 'allow',
      pwd: 'allow',
    },
  },
});

const env = {
  ...process.env,
  MIMOCODE_CONFIG_CONTENT: configContent,
  MIMOCODE_DISABLE_AUTOUPDATE: 'true',
  MIMOCODE_DISABLE_SHARE: 'true',
};

const args = [
  'run',
  'Run the attached review prompt exactly. Output only the final review.',
  '--file',
  promptFile,
  '--model',
  model,
  '--agent',
  'mimo-review',
  '--format',
  'default',
  '--title',
  'mimo structural review',
];

process.stderr.write(`[mimo-gate] ${mimo} run <review prompt> --model ${model} --agent mimo-review\n`);
process.stderr.write(`[mimo-gate] transcript -> ${logFile}\n`);

const res = spawnSync(mimo, args, {
  encoding: 'utf8',
  shell: isWin,
  env,
  maxBuffer: 64 * 1024 * 1024,
  timeout: timeoutMs,
  killSignal: 'SIGKILL',
});

const out = res.stdout || '';
const err = res.stderr || '';
writeFileSync(logFile, `${out}\n----- stderr -----\n${err}`);

if (res.error && res.error.code === 'ENOENT') {
  emitSkip('MiMo CLI not installed (run: npm install -g @mimo-ai/cli, then `mimo auth login`).');
}

if (res.error && (res.error.code === 'ETIMEDOUT' || res.signal)) {
  emitSkip(
    `MiMo review timed out after ${timeoutMs}ms (raise MIMO_REVIEW_TIMEOUT_MS or set MIMO_REVIEW_GATE=off). Transcript: ${logFile}`,
  );
}

const combined = `${out}\n${err}`;
// Skip cleanly on any provider/auth/model failure — key off "no real verdict"
// rather than "empty stdout", since mimo can emit TUI noise to stdout on error.
const hasVerdict = /APPROVE( WITH COMMENTS)?|REQUEST CHANGES/i.test(out);
if (
  !hasVerdict &&
  /not (logged in|authenticated)|unauthorized|api key|auth login|no model configured|no provider|ProviderModelNotFoundError|Model not found/i.test(
    combined,
  )
) {
  emitSkip(
    'MiMo not authenticated, provider not configured, or requested model unavailable — run `mimo providers login`, confirm `mimo models mimo` lists the model, or set MIMO_REVIEW_GATE=off.',
  );
}

if (out.trim()) {
  process.stdout.write('===== MIMO REVIEW (final message) =====\n');
  if (modelNote) process.stdout.write(modelNote + '\n\n');
  process.stdout.write(out.trim() + '\n');
  process.stdout.write('===== END MIMO REVIEW =====\n');
} else {
  process.stderr.write(`[mimo-gate] No review produced (exit ${res.status}). See ${logFile}\n`);
}

process.exit(res.status ?? 1);
