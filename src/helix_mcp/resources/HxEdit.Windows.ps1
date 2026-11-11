#requires -Version 5.1
<#
.SYNOPSIS
Finds, starts, or inspects HX Edit on Windows without changing presets.

.DESCRIPTION
PowerShell 5.1-compatible bootstrap and read-only UI Automation diagnostic.
Discover checks running HX Edit processes and matching uninstall registry entries.
Launch starts only the HX Edit executable explicitly supplied by the caller.
Inspect reads UI Automation windows belonging to an explicitly selected HX Edit
process. It does not import presets, send keystrokes, click controls, or change
window focus. Actual HX Edit UI identifiers must be observed before an importer
can be implemented. This script has NOT been verified on a Windows / Helix Floor
installation in the development environment.

Diagnostics are UTF-8 JSON without a BOM. Default location:
  <project>\artifacts\diagnostics\
Only HX Edit process/window details and matching HX Edit installer metadata are
recorded. Diagnostics can contain your local installation path and preset names;
review the file before sharing it.

.PARAMETER Mode
Discover (default), Launch, or Inspect. There is deliberately no Import mode.

.PARAMETER ExecutablePath
An existing HX Edit.exe or HXEdit.exe path, explicitly supplied for Launch.
Registry findings are never executed automatically.

.PARAMETER ProcessId
The running HX Edit process ID to inspect. Required for Inspect. Discover lists
candidate process IDs in its diagnostic JSON.

.PARAMETER OutputPath
Optional .json output path. Relative paths are resolved against the project root.
Existing files are not overwritten.

.PARAMETER MaxNodes
Maximum total number of UI Automation nodes recorded (default 1500).

.PARAMETER MaxDepth
Maximum descendant depth for each top-level window (default 8; roots are depth 0).

.PARAMETER TimeoutSeconds
Inspection or launch wait budget in seconds (default 20). Inspection runs in a
separate PowerShell job so the caller can stop waiting for a stalled UI provider.

.EXAMPLE
powershell.exe -NoProfile -File .\scripts\HxEdit.Windows.ps1 -Mode Discover

.EXAMPLE
powershell.exe -NoProfile -File .\scripts\HxEdit.Windows.ps1 -Mode Launch -ExecutablePath "C:\path\to\HX Edit.exe"

.EXAMPLE
powershell.exe -NoProfile -File .\scripts\HxEdit.Windows.ps1 -Mode Inspect -ProcessId 1234 -TimeoutSeconds 20

.NOTES
No execution-policy changes, privilege escalation, input simulation, downloads,
registry writes, or changes to HX Edit / Helix presets are performed.
UI Automation coverage depends on the installed HX Edit version and its provider.
An empty or incomplete tree is diagnostic evidence, not proof that a control does
not exist. No AutomationId, control coordinate, or Home-key behavior is assumed.

Sources:
https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-usefortesting
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getforegroundwindow
https://line6.com/support/manuals/hxedit/
#>
[CmdletBinding()]
param(
    [ValidateSet('Discover', 'Launch', 'Inspect')]
    [string]$Mode = 'Discover',

    [string]$ExecutablePath,

    [ValidateRange(0, 2147483647)]
    [int]$ProcessId = 0,

    [string]$OutputPath,

    [ValidateRange(1, 10000)]
    [int]$MaxNodes = 1500,

    [ValidateRange(0, 20)]
    [int]$MaxDepth = 8,

    [ValidateRange(5, 120)]
    [int]$TimeoutSeconds = 20
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This script requires Windows. No HX Edit operations were performed.'
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$hxProcessPattern = '^(?i:HX\s*Edit)$'
$hxExecutablePattern = '^(?i:HX\s*Edit\.exe)$'
$hxDisplayPattern = '^(?i:(?:Line\s*6\s+)?HX\s*Edit)(?:\s|$)'

function Get-HxProcessRecord {
    param([Parameter(Mandatory = $true)]$Process)

    $record = [ordered]@{
        ProcessId = $Process.Id
        ProcessName = $Process.ProcessName
        ExecutablePath = $null
        FileVersion = $null
        StartTimeUtcTicks = $null
        MainWindowHandle = $null
        MainWindowTitle = $null
        ReadErrors = @()
    }
    try {
        $Process.Refresh()
        $record.MainWindowHandle = $Process.MainWindowHandle.ToInt64()
        $record.MainWindowTitle = $Process.MainWindowTitle
    }
    catch { $record.ReadErrors += 'Window information unavailable.' }
    try { $record.ExecutablePath = $Process.Path }
    catch { $record.ReadErrors += 'Executable path unavailable.' }
    try { $record.FileVersion = $Process.MainModule.FileVersionInfo.FileVersion }
    catch { $record.ReadErrors += 'File version unavailable.' }
    try { $record.StartTimeUtcTicks = $Process.StartTime.ToUniversalTime().Ticks }
    catch { $record.ReadErrors += 'Process start time unavailable.' }
    return [pscustomobject]$record
}

function Get-HxProcesses {
    $found = @(Get-Process -Name 'HX Edit', 'HXEdit' -ErrorAction SilentlyContinue)
    foreach ($candidate in $found) {
        if ($candidate.ProcessName -match $hxProcessPattern) {
            Get-HxProcessRecord -Process $candidate
        }
    }
}

function Get-RegistryString {
    param($Entry, [string]$Name)
    $property = $Entry.PSObject.Properties[$Name]
    if ($null -ne $property -and $null -ne $property.Value) {
        return [string]$property.Value
    }
    return $null
}

function Get-HxInstallations {
    $registryRoots = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKCU:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    $seen = @{}
    foreach ($registryRoot in $registryRoots) {
        if (-not (Test-Path -LiteralPath $registryRoot -ErrorAction SilentlyContinue)) {
            continue
        }
        $keys = @(Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue)
        foreach ($key in $keys) {
            try { $entry = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction Stop }
            catch { continue }
            $displayName = Get-RegistryString -Entry $entry -Name 'DisplayName'
            if ([string]::IsNullOrWhiteSpace($displayName) -or $displayName -notmatch $hxDisplayPattern) {
                continue
            }
            if ($seen.ContainsKey($key.Name)) { continue }
            $seen[$key.Name] = $true

            # Report installer metadata only; do not invoke uninstall strings or
            # infer and execute paths from registry values.
            [pscustomobject][ordered]@{
                RegistryPath = $key.Name
                DisplayName = $displayName
                DisplayVersion = Get-RegistryString -Entry $entry -Name 'DisplayVersion'
                Publisher = Get-RegistryString -Entry $entry -Name 'Publisher'
                InstallLocation = Get-RegistryString -Entry $entry -Name 'InstallLocation'
                DisplayIcon = Get-RegistryString -Entry $entry -Name 'DisplayIcon'
            }
        }
    }
}

function Resolve-HxExecutable {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'Launch requires -ExecutablePath pointing to your installed HX Edit executable.'
    }
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($item.PSIsContainer -or $item.Name -notmatch $hxExecutablePattern) {
        throw 'ExecutablePath must point to an existing HX Edit.exe or HXEdit.exe file.'
    }
    return $item.FullName
}

function Save-Diagnostic {
    param([Parameter(Mandatory = $true)]$Value, [string]$RequestedPath)

    if ([string]::IsNullOrWhiteSpace($RequestedPath)) {
        $timestamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
        $suffix = [Guid]::NewGuid().ToString('N').Substring(0, 8)
        $filename = 'hx-edit-{0}-{1}-{2}.json' -f $Mode.ToLowerInvariant(), $timestamp, $suffix
        $RequestedPath = Join-Path (Join-Path $projectRoot 'artifacts\diagnostics') $filename
    }
    elseif (-not [IO.Path]::IsPathRooted($RequestedPath)) {
        $RequestedPath = Join-Path $projectRoot $RequestedPath
    }

    $destination = [IO.Path]::GetFullPath($RequestedPath)
    if ([IO.Path]::GetExtension($destination) -ine '.json') {
        throw 'OutputPath must have a .json extension.'
    }
    $directory = [IO.Path]::GetDirectoryName($destination)
    [void][IO.Directory]::CreateDirectory($directory)
    $json = $Value | ConvertTo-Json -Depth 40
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $bytes = $encoding.GetBytes($json + [Environment]::NewLine)
    $stream = $null
    try {
        # CreateNew also protects against a race when the destination exists.
        $stream = [IO.File]::Open($destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $stream.Write($bytes, 0, $bytes.Length)
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
    return $destination
}

function Invoke-HxReadOnlyInspection {
    param(
        [Parameter(Mandatory = $true)]$Target,
        [int]$NodeLimit,
        [int]$DepthLimit,
        [int]$TimeLimit
    )

    # A job keeps slow or unresponsive accessibility-provider calls out of the
    # caller's runspace. The worker also observes a cooperative time budget.
    $worker = {
        param($TargetProcessId, $ExpectedStartTicks, $NodeLimit, $DepthLimit, $TimeLimit)
        Set-StrictMode -Version 2.0
        $ErrorActionPreference = 'Stop'
        $clock = [Diagnostics.Stopwatch]::StartNew()
        $nodes = New-Object 'System.Collections.Generic.List[object]'
        $rootIds = New-Object 'System.Collections.Generic.List[int]'
        $warnings = New-Object 'System.Collections.Generic.List[string]'
        $partial = $false
        $skippedForeignNodes = 0
        $status = 'ok'
        $foregroundIsTarget = $false

        try {
            $targetProcess = Get-Process -Id $TargetProcessId -ErrorAction Stop
            if ($targetProcess.ProcessName -notmatch '^(?i:HX\s*Edit)$') {
                throw 'The selected process is no longer HX Edit.'
            }
            if ($null -ne $ExpectedStartTicks -and
                $targetProcess.StartTime.ToUniversalTime().Ticks -ne [long]$ExpectedStartTicks) {
                throw 'The selected process restarted. Run Discover again.'
            }

            Add-Type -AssemblyName UIAutomationClient
            Add-Type -AssemblyName UIAutomationTypes
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace HxEditDiagnostic {
    public static class ReadOnlyNative {
        [DllImport("user32.dll")]
        public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")]
        public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
    }
}
'@
            $foregroundHandle = [HxEditDiagnostic.ReadOnlyNative]::GetForegroundWindow()
            $foregroundProcessId = [uint32]0
            [void][HxEditDiagnostic.ReadOnlyNative]::GetWindowThreadProcessId($foregroundHandle, [ref]$foregroundProcessId)
            $foregroundIsTarget = ($foregroundProcessId -eq [uint32]$TargetProcessId)

            $desktop = [System.Windows.Automation.AutomationElement]::RootElement
            $conditionArguments = @(
                [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
                [int]$TargetProcessId
            )
            $condition = New-Object -TypeName System.Windows.Automation.PropertyCondition -ArgumentList $conditionArguments
            $rootWindows = $desktop.FindAll([System.Windows.Automation.TreeScope]::Children, $condition)
            $queue = New-Object 'System.Collections.Generic.Queue[object]'
            foreach ($window in $rootWindows) {
                if ($queue.Count -ge $NodeLimit) { $partial = $true; break }
                $queue.Enqueue([pscustomobject]@{ Element = $window; ParentId = $null; Depth = 0 })
            }
            $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker

            while ($queue.Count -gt 0 -and $nodes.Count -lt $NodeLimit) {
                if ($clock.Elapsed.TotalSeconds -ge [Math]::Max(1, $TimeLimit - 2)) {
                    $partial = $true
                    $status = 'time_limit'
                    break
                }
                $pending = $queue.Dequeue()
                $element = $pending.Element
                try {
                    # Check ownership before reading names or any other UI data.
                    $current = $element.Current
                    if ($current.ProcessId -ne [int]$TargetProcessId) {
                        $skippedForeignNodes++
                        continue
                    }
                    $nodeId = $nodes.Count
                    $rect = $current.BoundingRectangle
                    $bounds = $null
                    if (-not $rect.IsEmpty -and
                        -not [double]::IsInfinity($rect.X) -and
                        -not [double]::IsInfinity($rect.Y) -and
                        -not [double]::IsInfinity($rect.Width) -and
                        -not [double]::IsInfinity($rect.Height) -and
                        -not [double]::IsNaN($rect.X) -and
                        -not [double]::IsNaN($rect.Y) -and
                        -not [double]::IsNaN($rect.Width) -and
                        -not [double]::IsNaN($rect.Height)) {
                        $bounds = [ordered]@{
                            X = [Math]::Round($rect.X, 2)
                            Y = [Math]::Round($rect.Y, 2)
                            Width = [Math]::Round($rect.Width, 2)
                            Height = [Math]::Round($rect.Height, 2)
                        }
                    }
                    $selected = $null
                    $pattern = $null
                    $supportedPatterns = @()
                    try {
                        $supportedPatterns = @($element.GetSupportedPatterns() | ForEach-Object { $_.ProgrammaticName })
                        if ($element.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
                            $selected = ([System.Windows.Automation.SelectionItemPattern]$pattern).Current.IsSelected
                        }
                    }
                    catch {
                        if ($warnings.Count -lt 20) {
                            $warnings.Add('Some control patterns became unavailable during inspection.')
                        }
                        $partial = $true
                    }
                    $record = [pscustomobject][ordered]@{
                        Id = $nodeId
                        ParentId = $pending.ParentId
                        Depth = $pending.Depth
                        ProcessId = $current.ProcessId
                        Name = $current.Name
                        AutomationId = $current.AutomationId
                        ControlType = $current.ControlType.ProgrammaticName
                        ClassName = $current.ClassName
                        FrameworkId = $current.FrameworkId
                        IsEnabled = $current.IsEnabled
                        IsOffscreen = $current.IsOffscreen
                        HasKeyboardFocus = $current.HasKeyboardFocus
                        IsKeyboardFocusable = $current.IsKeyboardFocusable
                        NativeWindowHandle = $current.NativeWindowHandle
                        IsForegroundWindow = ($current.NativeWindowHandle -ne 0 -and
                            [long]$current.NativeWindowHandle -eq $foregroundHandle.ToInt64())
                        BoundingRectangle = $bounds
                        SelectionItemIsSelected = $selected
                        SupportedPatterns = $supportedPatterns
                    }
                    $nodes.Add($record)
                    if ($null -eq $pending.ParentId) { $rootIds.Add($nodeId) }

                    if ($pending.Depth -ge $DepthLimit) {
                        # Descendants beyond this limit are intentionally omitted.
                        if ($null -ne $walker.GetFirstChild($element)) { $partial = $true }
                        continue
                    }
                    $child = $walker.GetFirstChild($element)
                    while ($null -ne $child) {
                        if ($nodes.Count + $queue.Count -ge $NodeLimit) {
                            $partial = $true
                            break
                        }
                        if ($clock.Elapsed.TotalSeconds -ge [Math]::Max(1, $TimeLimit - 2)) {
                            $partial = $true
                            $status = 'time_limit'
                            break
                        }
                        $queue.Enqueue([pscustomobject]@{
                            Element = $child
                            ParentId = $nodeId
                            Depth = $pending.Depth + 1
                        })
                        $child = $walker.GetNextSibling($child)
                    }
                }
                catch {
                    $partial = $true
                    if ($warnings.Count -lt 20) {
                        $warnings.Add('An HX Edit element changed or could not be read: ' + $_.Exception.GetType().Name)
                    }
                }
            }
            if ($queue.Count -gt 0) { $partial = $true }
            if ($rootWindows.Count -eq 0) {
                $status = 'no_windows'
                $warnings.Add('No top-level HX Edit windows were exposed for the selected process.')
            }
        }
        catch {
            $status = 'error'
            $partial = $true
            $warnings.Add('Inspection could not complete: ' + $_.Exception.Message)
        }

        [pscustomobject][ordered]@{
            Status = $status
            ReadOnly = $true
            ProcessId = [int]$TargetProcessId
            Partial = $partial
            MaxNodes = $NodeLimit
            MaxDepth = $DepthLimit
            ElapsedSeconds = [Math]::Round($clock.Elapsed.TotalSeconds, 3)
            ForegroundIsTargetProcess = $foregroundIsTarget
            RootWindowIds = @($rootIds.ToArray())
            Nodes = @($nodes.ToArray())
            SkippedForeignProcessNodes = $skippedForeignNodes
            Warnings = @($warnings.ToArray() | Select-Object -Unique)
        } | ConvertTo-Json -Depth 35 -Compress
    }

    $job = $null
    try {
        $job = Start-Job -ScriptBlock $worker -ArgumentList @(
            $Target.ProcessId, $Target.StartTimeUtcTicks, $NodeLimit, $DepthLimit, $TimeLimit
        )
        $finished = Wait-Job -Job $job -Timeout $TimeLimit
        if ($null -eq $finished) {
            Stop-Job -Job $job -ErrorAction SilentlyContinue
            return [pscustomobject][ordered]@{
                Status = 'timeout'
                ReadOnly = $true
                ProcessId = $Target.ProcessId
                Partial = $true
                MaxNodes = $NodeLimit
                MaxDepth = $DepthLimit
                RootWindowIds = @()
                Nodes = @()
                Warnings = @('UI Automation did not finish within the time budget. The diagnostic job was stopped; HX Edit was not stopped.')
            }
        }
        $output = @(Receive-Job -Job $job -ErrorAction Stop)
        if ($output.Count -ne 1 -or -not ($output[0] -is [string])) {
            throw 'The UI Automation worker did not return one diagnostic JSON object.'
        }
        return ($output[0] | ConvertFrom-Json)
    }
    finally {
        if ($null -ne $job) {
            if ($job.State -eq 'Running') { Stop-Job -Job $job -ErrorAction SilentlyContinue }
            Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        }
    }
}

$report = [ordered]@{
    SchemaVersion = 1
    Tool = 'Helix Tone Workbench / HX Edit Windows diagnostic'
    ToolVersion = '0.1.0'
    GeneratedUtc = [DateTime]::UtcNow.ToString('o')
    Mode = $Mode
    Status = 'ok'
    WindowsHardwareVerified = $false
    PresetsModified = $false
    PowerShellVersion = $PSVersionTable.PSVersion.ToString()
    PowerShellIs64Bit = [Environment]::Is64BitProcess
    Installations = @()
    Processes = @()
    Inspection = $null
    Warnings = @()
}
$exitCode = 0

try {
    switch ($Mode) {
        'Discover' {
            $report.Installations = @(Get-HxInstallations)
            $report.Processes = @(Get-HxProcesses)
            if (-not [Environment]::Is64BitProcess -and [Environment]::Is64BitOperatingSystem) {
                $report.Warnings += 'Using 32-bit PowerShell on 64-bit Windows may omit registry entries. Prefer 64-bit Windows PowerShell.'
            }
        }
        'Launch' {
            $resolvedExecutable = Resolve-HxExecutable -Path $ExecutablePath
            $started = Start-Process -FilePath $resolvedExecutable -PassThru
            $report['RequestedExecutablePath'] = $resolvedExecutable
            $report['StartedProcessId'] = $started.Id
            $waitClock = [Diagnostics.Stopwatch]::StartNew()
            do {
                $report.Processes = @(Get-HxProcesses)
                $visibleProcesses = @($report.Processes | Where-Object { $_.MainWindowHandle -ne 0 -and $null -ne $_.MainWindowHandle })
                if ($visibleProcesses.Count -gt 0) { break }
                Start-Sleep -Milliseconds 200
            } while ($waitClock.Elapsed.TotalSeconds -lt $TimeoutSeconds)
            if ($report.Processes.Count -eq 0) {
                $report.Status = 'launch_unconfirmed'
                $report.Warnings += 'No HX Edit process was detected after launch. Check the application on Windows.'
                $exitCode = 2
            }
            elseif ($visibleProcesses.Count -eq 0) {
                $report.Status = 'launched_without_visible_main_window'
                $report.Warnings += 'HX Edit is running, but a main window was not observed within the wait budget.'
            }
        }
        'Inspect' {
            if ($ProcessId -le 0) { throw 'Inspect requires -ProcessId from the Discover result.' }
            $targetProcess = Get-Process -Id $ProcessId -ErrorAction Stop
            if ($targetProcess.ProcessName -notmatch $hxProcessPattern) {
                throw 'The selected ProcessId does not belong to HX Edit. No window data was inspected.'
            }
            $target = Get-HxProcessRecord -Process $targetProcess
            $report.Processes = @($target)
            $report.Inspection = Invoke-HxReadOnlyInspection -Target $target -NodeLimit $MaxNodes -DepthLimit $MaxDepth -TimeLimit $TimeoutSeconds
            $report.Status = $report.Inspection.Status
            if ($report.Status -ne 'ok') { $exitCode = 2 }
        }
    }
}
catch {
    $report.Status = 'error'
    $report.Warnings += $_.Exception.Message
    $exitCode = 1
}

$savedPath = Save-Diagnostic -Value ([pscustomobject]$report) -RequestedPath $OutputPath
$nodeCount = 0
if ($null -ne $report.Inspection) { $nodeCount = @($report.Inspection.Nodes).Count }
[pscustomobject][ordered]@{
    Mode = $Mode
    Status = $report.Status
    DiagnosticPath = $savedPath
    ProcessCount = @($report.Processes).Count
    InstallationCount = @($report.Installations).Count
    NodeCount = $nodeCount
    PresetsModified = $false
} | ConvertTo-Json -Depth 4

exit $exitCode
