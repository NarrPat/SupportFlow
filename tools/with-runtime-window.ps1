# Invoke only synchronous work. The caller checks native exit codes and returns
# normally only after draining its registered resources. On uncertainty, throw.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$CoordinationDirectory,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Purpose,
    [Parameter(Mandatory)][scriptblock]$Work,
    [string[]]$Resources = @('supportflow project-local development environment'),
    [ValidateRange(1, 240)][int]$MaxMinutes = 45,
    [ValidatePattern('^sf-[a-z0-9-]{1,42}$')][string]$ComposeProject = 'sf-local'
)
$ErrorActionPreference = 'Stop'
$coord = (Get-Item -LiteralPath $CoordinationDirectory).FullName
$lockPath = Join-Path $coord 'runtime-exclusive.lock'
$nonce = [guid]::NewGuid().ToString('N')
$recordPath = Join-Path $coord "SF-window-$nonce.json"
$start = [DateTimeOffset]::UtcNow
$window = [ordered]@{
    owner = 'supportflow'; nonce = $nonce; purpose = $Purpose
    started_at = $start.ToString('o'); max_deadline = $start.AddMinutes($MaxMinutes).ToString('o')
    project_root = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..')).FullName
    compose_project = $ComposeProject; resources = $Resources
    stop_and_drain = 'Finish synchronous work; verify registered children/services have drained before returning.'
}

function Write-JsonAndClose($Stream, $Value) {
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json $Value -Depth 5))
        $Stream.Write($bytes, 0, $bytes.Length)
        $Stream.Flush($true)
    } finally { $Stream.Dispose() }
}
function Test-OwnWindow {
    try {
        $current = [IO.File]::ReadAllText($lockPath) | ConvertFrom-Json
        return ($current.owner -ceq 'supportflow' -and $current.nonce -ceq $nonce)
    } catch { return $false }
}

# CreateNew also prevents an apparently expired lock from being overwritten.
try {
    $handle = [IO.File]::Open($lockPath, 'CreateNew', 'Write', 'None')
} catch {
    if ([IO.File]::Exists($lockPath)) { throw "SF_WINDOW_BUSY: $lockPath" }
    throw
}

$drained = $false
try {
    Write-JsonAndClose $handle $window
    Write-Information "SF_WINDOW_ACQUIRED $nonce" -InformationAction Continue
    # Pass a timestamp, never the mutable ownership record. The callback enforces
    # its deadline; killing it here could leave a child still writing to disk.
    & $Work ($start.AddMinutes($MaxMinutes))
    $drained = $true
} finally {
    $owned = Test-OwnWindow
    $window['ended_at'] = [DateTimeOffset]::UtcNow.ToString('o')
    $window['outcome'] = if (!$owned) { 'ownership_unconfirmed' }
        elseif (!$drained) { 'drain_unconfirmed' } else { 'drained' }
    $record = [IO.File]::Open($recordPath, 'CreateNew', 'Write', 'None')
    Write-JsonAndClose $record $window
    if ($owned -and $drained -and (Test-OwnWindow)) {
        Remove-Item -LiteralPath $lockPath -ErrorAction Stop
        Write-Information "SF_WINDOW_RELEASED $recordPath" -InformationAction Continue
    } elseif ($drained) {
        throw "SF_WINDOW_OWNERSHIP_UNCONFIRMED: inspect $recordPath and $lockPath"
    } else {
        Write-Warning "SF_WINDOW_RETAINED: inspect $recordPath and verify drain at $lockPath"
    }
}
