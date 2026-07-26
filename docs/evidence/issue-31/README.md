# Issue #31 — foreground-activation evidence

Records produced by [`tools/foreground-probe.ps1`](../../../tools/foreground-probe.ps1)
under the acceptance protocol in issue #31 §D.

`baseline-*` = **pre-fix** build (installed 0.3.0, `%LOCALAPPDATA%\Filmpaw\filmpaw.exe`),
where the folder is opened by the sidecar. `fixed-*` = post-fix build.

## Why "the window is visible" is not the test

Every baseline trial has a window sitting on the target — visible, not minimized —
while `GetForegroundWindow()` stays on Filmpaw for the full 5 s. Had the criterion
been "a visible window shows the target", **all six baseline trials would have
passed while the bug was plainly present**. That is why §D3 requires the
FOREGROUND window to resolve to the target path in every stable sample.

## Baseline result (2026-07-26, pre-fix)

| trial | mode | longest consecutive OK | target windows seen | verdict |
|---|---|---|---|---|
| baseline-single-1 | single | 0 / 9 | 1 (hwnd 2621798) | FAIL |
| baseline-single-2 | single | 0 / 9 | 1 (hwnd 856846) | FAIL |
| baseline-single-3 | single | 0 / 9 | 1 (hwnd 1904032) | FAIL |
| baseline-dual-1 | dual | 0 / 9 | 2 (2430050, 723834) | FAIL |
| baseline-dual-2 | dual | 0 / 9 | 2 (660084, 463504) | FAIL |
| baseline-dual-3 | dual | 0 / 9 | 2 (463636, 854906) | FAIL |

single **0/3**, dual **0/3** — clears the §D4 bar of ≥2/3 failing, so the
foreground-lock hypothesis stands and the fix may proceed.

Targets used:
- single: `\Ant\Video Station\女优VIII\Lisa`
- dual: `C:\Downloads\写真\胡桃さくら,新井リマ` + `\Koala\Personal-Drive\Video Station\女优 I\胡桃さくら`

The dual target deliberately keeps the **comma** in the local folder name, so these
records double as a live #28 (comma truncation) regression check — both windows
landed on the correct paths.

## Reading a record

Each sample carries the foreground window (`Hwnd`/`Pid`/`Process`/`Class`) and
`TargetWindows` — every window currently on a target, foreground or not. That
second field is what separates "opened but left behind" (the bug) from "the click
missed and nothing happened" (a void trial); the probe warns explicitly when no
target window ever appears.
