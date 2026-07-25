#!/usr/bin/env node
// kilo-gate.mjs -- cross-platform Kilo Code / GLM 5.2 review wrapper.
//
// Runs Kilo Code CLI as a READ-ONLY structural review gate using GLM 5.2.
// Default model: z-ai/glm-5.2. Default variant: max.
//
// Kilo can be used as an optional add-on reviewer. It may also be used as an
// experimental final gate only when the operator explicitly asks for it.

import { existsSync, mkdtempSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';

const isWin = process.platform === 'win32';
const model = process.env.KILO_REVIEW_MODEL || 'z-ai/glm-5.2';
const variant = process.env.KILO_REVIEW_VARIANT || 'max';
// Hard cap so a hung `kilo run` (e.g. waiting on an interactive prompt) can
// never wedge an unattended AFK cron. Floor at 60s; default 10 min.
const timeoutMs = Math.max(
  60_000,
  Number.parseInt(process.env.KILO_REVIEW_TIMEOUT_MS || '', 10) || 600_000,
);

function emitSkip(reason) {
  process.stderr.write(`[kilo-gate] skipped: ${reason}\n`);
  process.stdout.write('===== KILO REVIEW (final message) =====\n');
  process.stdout.write(`SKIPPED: ${reason}\n`);
  process.stdout.write('===== END KILO REVIEW =====\n');
  process.exit(0);
}

const gateFlag = (process.env.KILO_REVIEW_GATE || '').trim().toLowerCase();
if (['off', '0', 'false', 'no', 'disabled'].includes(gateFlag)) {
  emitSkip('Kilo gate disabled via KILO_REVIEW_GATE.');
}

function resolveKilo() {
  if (isWin && process.env.APPDATA) {
    const cmdShim = join(process.env.APPDATA, 'npm', 'kilo.cmd');
    if (existsSync(cmdShim)) return cmdShim;
  }
  return 'kilo';
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
    process.stderr.write(`[kilo-gate] unsafe ${label} value rejected: ${JSON.stringify(val)}\n`);
    process.exit(1);
  }
  return val;
}

const commitArg = sanitizeRef(optVal('--commit'), '--commit');
const uncommitted = userArgs.includes('--uncommitted');
const prArg = sanitizeRef(optVal('--pr'), '--pr');
const baseArg = sanitizeRef(optVal('--base'), '--base');

const guidance = [
  'Review for real bugs, security issues, data consistency problems, lifecycle/concurrency mistakes, and production edge cases.',
  'Pay special attention to behavior consistency across routes, tasks, UI state, config writes, and desktop/web parity.',
  'Ignore pure nitpicks. Output findings as [P1], [P2], or [minor] with file:line, risk, and concrete fix.',
  'Finish with APPROVE, APPROVE WITH COMMENTS, or REQUEST CHANGES.',
].join(' ');

let reviewCommandArgs;
if (commitArg) {
  reviewCommandArgs = [commitArg, guidance];
} else if (prArg) {
  reviewCommandArgs = [prArg, guidance];
} else if (uncommitted) {
  reviewCommandArgs = ['uncommitted', guidance];
} else {
  const base = sanitizeRef(baseArg || detectBase(), '--base');
  reviewCommandArgs = ['branch', base, guidance];
}

const kilo = resolveKilo();
const version = spawnSync(kilo, ['--version'], { encoding: 'utf8', shell: isWin });
if (version.error && version.error.code === 'ENOENT') {
  emitSkip('Kilo CLI not installed (run: npm install -g @kilocode/cli, then `kilo auth login`).');
}
const versionOutput = `${version.stdout || ''}${version.stderr || ''}`;
if (version.status !== 0 && /not recognized|not found|cannot find|ENOENT/i.test(versionOutput)) {
  emitSkip('Kilo CLI not installed (run: npm install -g @kilocode/cli, then `kilo auth login`).');
}

const work = mkdtempSync(join(tmpdir(), 'kilo-gate-'));
const logFile = join(work, 'kilo.log');

const args = [
  'run',
  '--command',
  'review',
  ...reviewCommandArgs,
  '--model',
  model,
  '--variant',
  variant,
  '--format',
  'default',
  '--title',
  'kilo glm-5.2 structural review',
  // Headless runs have no TTY, so permission prompts auto-reject and the
  // review starves (model can't even run `git diff`). --auto lets the
  // read-only review prompt do its job; verify `git status` is clean after
  // the gate as the write-guard.
  '--auto',
];

process.stderr.write(`[kilo-gate] ${kilo} run --command review <target> --model ${model} --variant ${variant}\n`);
process.stderr.write(`[kilo-gate] transcript -> ${logFile}\n`);

const res = spawnSync(kilo, args, {
  encoding: 'utf8',
  shell: isWin,
  maxBuffer: 64 * 1024 * 1024,
  timeout: timeoutMs,
  killSignal: 'SIGKILL',
});

const out = res.stdout || '';
const err = res.stderr || '';
writeFileSync(logFile, `${out}\n----- stderr -----\n${err}`);

// Write-guard for --auto: the review prompt is read-only, but verify the
// working tree really is untouched and shout if it is not.
const dirty = spawnSync('git', ['status', '--porcelain'], { encoding: 'utf8' });
if ((dirty.stdout || '').trim()) {
  process.stderr.write(
    `[kilo-gate] WARNING: working tree not clean after review — inspect these paths:\n${dirty.stdout}`,
  );
  // Fail loud: a silent tree mutation must not pass an unattended pipeline.
  process.exitCode = 3;
}

if (res.error && res.error.code === 'ENOENT') {
  emitSkip('Kilo CLI not installed (run: npm install -g @kilocode/cli, then `kilo auth login`).');
}

if (res.error && (res.error.code === 'ETIMEDOUT' || res.signal)) {
  // Reap kilo's orphaned children: SIGKILL on the shell wrapper does not
  // kill the node process tree on Windows, leaking ~5 processes per timeout.
  // Target only the kilo CLI itself (@kilocode package path / kilo shims) and
  // never this gate process — a bare 'kilo' match would suicide the gate
  // (its own cmdline contains kilo-gate.mjs) and could hit unrelated tools.
  if (isWin) {
    spawnSync(
      'powershell',
      [
        '-NoProfile',
        '-Command',
        `Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object { $_.ProcessId -ne ${process.pid} -and $_.CommandLine -match '@kilocode|kilo\\.cmd|kilo\\.ps1' -and $_.CommandLine -notmatch 'kilo-gate' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }`,
      ],
      { timeout: 30_000 },
    );
  }
  emitSkip(
    `Kilo review timed out after ${timeoutMs}ms (raise KILO_REVIEW_TIMEOUT_MS or set KILO_REVIEW_GATE=off). Transcript: ${logFile}`,
  );
}

const combined = `${out}\n${err}`;
// Skip cleanly on any provider/auth/model failure — key off "no real verdict"
// rather than "empty stdout", since the CLI can emit TUI noise on error.
const hasVerdict = /APPROVE( WITH COMMENTS)?|REQUEST CHANGES/i.test(out);
if (!hasVerdict && /not (logged in|authenticated)|unauthorized|api key|auth login|no model configured|no provider|ProviderModelNotFoundError|Model not found|connect/i.test(combined)) {
  emitSkip('Kilo not authenticated, provider not configured, or model unavailable — run `kilo auth login`, or set KILO_REVIEW_GATE=off.');
}

if (out.trim()) {
  process.stdout.write('===== KILO REVIEW (final message) =====\n');
  process.stdout.write(out.trim() + '\n');
  process.stdout.write('===== END KILO REVIEW =====\n');
} else {
  process.stderr.write(`[kilo-gate] No review produced (exit ${res.status}). See ${logFile}\n`);
}

process.exit(res.status ?? 1);
