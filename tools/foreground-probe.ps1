<#
.SYNOPSIS
  Foreground-activation probe for issue #31 (Explorer window flashes in the taskbar
  instead of coming to the front).

.DESCRIPTION
  Implements the acceptance protocol defined in issue #31 §D. The point of this
  script is that "the Explorer window is visible" is NOT evidence of activation —
  a window sitting behind Filmpaw and blinking in the taskbar is visible too. So
  every stable sample must have GetForegroundWindow() resolve to the TARGET path.

  Protocol (§D2/§D3):
    - assert Filmpaw is foreground BEFORE the click (else the trial is void)
    - inject a REAL mouse click via SendInput. UI Automation's InvokePattern is
      deliberately NOT used to press the button: it raises no input event, so the
      foreground lock never engages and the baseline would falsely pass. UIA is
      used only to LOCATE the button.
    - sample at t = 0, 0.25, ... 5.00s (21 samples, both ends inclusive)
    - a trial passes if 9 CONSECUTIVE samples satisfy the foreground rule
      (9 samples span exactly 2.00s = 8 intervals x 250ms)

.PARAMETER Target
  One target path for a single-open trial, or two for a dual-open trial.

.PARAMETER ClickName
  Accessible name of the button to press inside the Filmpaw window. WebView2 only
  builds its UI Automation tree on demand, so this usually finds nothing in a Tauri
  window (FindAll for buttons returns 0) — use -ClickPoint in that case.

.PARAMETER ClickPoint
  "x,y" in SCREEN coordinates, for when the button is not reachable via UIA.

.PARAMETER Label
  Free-form label recorded in the output (e.g. "baseline-single").

.PARAMETER OutFile
  Where to write the JSON record that gets pasted into the PR.

.EXAMPLE
  .\foreground-probe.ps1 -Target '\\Eagle\Video Station\女优III\沙月恵奈' `
      -ClickName '打开文件夹' -Label baseline-single -OutFile baseline-single-1.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string[]]$Target,
    [string]$ClickName,
    [string]$ClickPoint,
    [Parameter(Mandatory = $true)][string]$Label,
    [Parameter(Mandatory = $true)][string]$OutFile
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

if ($Target.Count -lt 1 -or $Target.Count -gt 2) {
    throw "Target takes 1 path (single open) or 2 paths (dual open); got $($Target.Count)."
}
$mode = if ($Target.Count -eq 1) { 'single' } else { 'dual' }

Add-Type -Namespace Fp -Name Win -MemberDefinition @'
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid);
[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, System.Text.StringBuilder s, int n);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr extra);
[DllImport("mpr.dll", CharSet=CharSet.Unicode)] public static extern int WNetGetConnection(string local, System.Text.StringBuilder remote, ref int len);
'@

# --- §D1 path normalization -------------------------------------------------
# absolute -> backslashes -> strip trailing separator (drive root keeps its
# backslash) -> resolve a mapped drive letter back to its UNC form -> compare
# ordinal-ignore-case. Comparing a mapped drive against a UNC target would
# otherwise report a false mismatch.
function Normalize-Path([string]$p) {
    if ([string]::IsNullOrWhiteSpace($p)) { return '' }
    $p = $p.Trim().Replace('/', '\')
    if ($p -match '^[A-Za-z]:(\\|$)') {
        $sb = New-Object System.Text.StringBuilder 1024
        $len = $sb.Capacity
        if ([Fp.Win]::WNetGetConnection($p.Substring(0, 2), $sb, [ref]$len) -eq 0) {
            $p = $sb.ToString().TrimEnd('\') + $p.Substring(2)
        }
    }
    if ($p -match '^[A-Za-z]:\\$') { return $p.ToUpperInvariant() }   # drive root keeps the separator
    return $p.TrimEnd('\').ToUpperInvariant()
}

$targetsNorm = @($Target | ForEach-Object { Normalize-Path $_ })

# --- Explorer window enumeration -------------------------------------------
# Shell.Application is the only reliable HWND -> current folder mapping; the
# window title is localized and truncated, so it must not be used for matching.
$shell = New-Object -ComObject Shell.Application
function Get-ExplorerWindows {
    $out = @()
    foreach ($w in $shell.Windows()) {
        try {
            $path = $w.Document.Folder.Self.Path
            if (-not $path) { continue }
            $h = [IntPtr]$w.HWND
            $out += [pscustomobject]@{
                Hwnd      = [int64]$h
                Path      = $path
                Norm      = Normalize-Path $path
                Visible   = [Fp.Win]::IsWindowVisible($h)
                Minimized = [Fp.Win]::IsIconic($h)
            }
        } catch { }   # a window closing mid-enumeration must not kill the trial
    }
    return $out
}

function Get-Foreground {
    $h = [Fp.Win]::GetForegroundWindow()
    $pid_ = 0
    [void][Fp.Win]::GetWindowThreadProcessId($h, [ref]$pid_)
    $cls = New-Object System.Text.StringBuilder 256
    [void][Fp.Win]::GetClassName($h, $cls, $cls.Capacity)
    $name = try { (Get-Process -Id $pid_ -ErrorAction Stop).ProcessName } catch { '<gone>' }
    return [pscustomobject]@{
        Hwnd = [int64]$h; Pid = $pid_; Process = $name; Class = $cls.ToString()
        Visible = [Fp.Win]::IsWindowVisible($h); Minimized = [Fp.Win]::IsIconic($h)
    }
}

# --- pre-click assertion (§D2) ---------------------------------------------
$pre = Get-Foreground
if ($pre.Process -ne 'filmpaw') {
    throw "VOID TRIAL: Filmpaw must be foreground before the click, but '$($pre.Process)' was (hwnd=$($pre.Hwnd)). Focus the app and rerun."
}
Write-Host "[probe] pre-click foreground OK: $($pre.Process) hwnd=$($pre.Hwnd) class=$($pre.Class)"

# --- locate the button (UIA is used for LOCATION ONLY) ----------------------
if ($ClickPoint) {
    $xy = $ClickPoint -split '\s*,\s*'
    if ($xy.Count -ne 2) { throw "ClickPoint must be 'x,y' in screen coordinates." }
    $cx = [int]$xy[0]; $cy = [int]$xy[1]
    $how = "point"
} elseif ($ClickName) {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$pre.Hwnd)
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, $ClickName)
    $btn = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
    if ($null -eq $btn) {
        throw "Button '$ClickName' not found via UIA (WebView2 often exposes no tree) — pass -ClickPoint 'x,y' instead."
    }
    $pt = $btn.GetClickablePoint(); $cx = [int]$pt.X; $cy = [int]$pt.Y
    $how = "uia:$ClickName"
} else {
    throw "Pass either -ClickName or -ClickPoint."
}
Write-Host "[probe] click target ($cx,$cy) via $how"

# --- click: a REAL input event, not UIA Invoke ------------------------------
[void][Fp.Win]::SetCursorPos($cx, $cy)
Start-Sleep -Milliseconds 60
$sw = [System.Diagnostics.Stopwatch]::StartNew()
[Fp.Win]::mouse_event(0x0002, 0, 0, 0, [IntPtr]::Zero)   # LEFTDOWN
[Fp.Win]::mouse_event(0x0004, 0, 0, 0, [IntPtr]::Zero)   # LEFTUP
# From here on: NO further input, or the trial is void (§D2).

# --- sampling (§D2) ---------------------------------------------------------
$samples = @()
for ($i = 0; $i -le 20; $i++) {
    $due = $i * 250
    while ($sw.ElapsedMilliseconds -lt $due) { Start-Sleep -Milliseconds 5 }
    $fg = Get-Foreground
    $wins = Get-ExplorerWindows
    $fgWin = $wins | Where-Object { $_.Hwnd -eq $fg.Hwnd } | Select-Object -First 1
    $fgNorm = if ($fgWin) { $fgWin.Norm } else { '' }

    # §D3 foreground rule. Single: the FOREGROUND window must be the target.
    # Dual: foreground is one of the two AND both targets are simultaneously
    # present as visible, non-minimized windows — a single HWND navigating from
    # one target to the other leaves one target unshown and must not pass.
    $ok = $false
    if ($fgNorm -and $targetsNorm -contains $fgNorm -and $fg.Visible -and -not $fg.Minimized) {
        if ($mode -eq 'single') {
            $ok = $true
        } else {
            # Windows 11 Explorer is TABBED: Shell.Windows() lists every tab, and
            # sibling tabs share one HWND. Two targets landing as two tabs of the
            # same window means only one of them is actually on screen — so the
            # two matches must live in DISTINCT windows.
            $hwnds = @()
            foreach ($t in $targetsNorm) {
                $m = $wins | Where-Object { $_.Norm -eq $t -and $_.Visible -and -not $_.Minimized } |
                     Select-Object -First 1
                if ($m) { $hwnds += $m.Hwnd }
            }
            $ok = ($hwnds.Count -eq 2 -and ($hwnds | Select-Object -Unique).Count -eq 2)
        }
    }
    # Record every window sitting on a target, whether or not it is foreground.
    # Without this the record cannot tell "opened but left behind" (the bug) from
    # "the click missed and nothing opened" (a void trial).
    $hit = @($wins | Where-Object { $targetsNorm -contains $_.Norm } |
             ForEach-Object { [pscustomobject]@{ Hwnd = $_.Hwnd; Path = $_.Path; Visible = $_.Visible; Minimized = $_.Minimized } })
    $samples += [pscustomobject]@{
        T = $due / 1000.0; Hwnd = $fg.Hwnd; Pid = $fg.Pid; Process = $fg.Process
        Class = $fg.Class; Visible = $fg.Visible; Minimized = $fg.Minimized
        FgPath = $fgNorm; TargetWindows = $hit; Ok = $ok
    }
}

# --- verdict: 9 consecutive passing samples = 2.00s (§D2) -------------------
$run = 0; $best = 0
foreach ($s in $samples) {
    if ($s.Ok) { $run++; if ($run -gt $best) { $best = $run } } else { $run = 0 }
}
$pass = $best -ge 9

$record = [ordered]@{
    label = $Label; mode = $mode; targets = $Target; targetsNormalized = $targetsNorm
    click = @{ how = $how; x = $cx; y = $cy }; preClickForeground = $pre
    longestConsecutiveOk = $best; requiredConsecutive = 9; pass = $pass
    samples = $samples
}
$record | ConvertTo-Json -Depth 6 | Out-File -FilePath $OutFile -Encoding utf8
$samples |
    Select-Object T, Process, Class, Hwnd, Ok, FgPath,
        @{ n = 'TargetWins'; e = { ($_.TargetWindows | ForEach-Object { "$($_.Hwnd)$(if(-not $_.Visible){'/hidden'})$(if($_.Minimized){'/min'})" }) -join ',' } } |
    Format-Table -AutoSize | Out-String | Write-Host
# A trial where no window ever reached a target is a MISSED CLICK, not evidence.
if (-not ($samples | Where-Object { $_.TargetWindows.Count -gt 0 })) {
    Write-Host "[probe] WARNING: no window ever showed a target path — the click probably missed the button. Treat this trial as VOID, not as a failure."
}
Write-Host "[probe] $Label ($mode): longest consecutive OK = $best / 9 required -> $(if ($pass) {'PASS'} else {'FAIL'})"
Write-Host "[probe] record -> $OutFile"
if (-not $pass) { exit 1 }
