[CmdletBinding()]
param([switch]$PreflightOnly)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Docker([string[]]$Arguments) {
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments[0]) failed (exit $LASTEXITCODE)"
    }
}

$project = 'sf-local'
$composeFile = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'compose.yaml')).Path
$compose = @('compose', '-p', $project, '--project-directory', $PSScriptRoot, '-f', $composeFile)
$projectFilter = "label=com.docker.compose.project=$project"
$context = (Invoke-Docker @('context', 'show')).Trim()
$endpoint = (Invoke-Docker @('context', 'inspect', $context, '--format', '{{.Endpoints.docker.Host}}')).Trim()
foreach ($hostUri in @($endpoint, $env:DOCKER_HOST)) {
    if ($hostUri -and $hostUri -notmatch '^(npipe:////\./pipe/|unix:///)') {
        throw 'Runtime verification requires a local Docker engine.'
    }
}

$model = (Invoke-Docker ($compose + @('config', '--format', 'json')) | Out-String) | ConvertFrom-Json
$services = @($model.services.PSObject.Properties.Name | Sort-Object)
if ($model.name -ne $project -or ($services -join ',') -ne 'postgres,redis' -or !$model.networks.default.internal) {
    throw 'Unexpected project, services or network in data-services Compose file.'
}
foreach ($name in $services) {
    $service = $model.services.$name
    if ($service.PSObject.Properties['ports'] -or $service.PSObject.Properties['container_name'] -or
        $service.platform -ne 'linux/amd64' -or $service.image -notmatch '@sha256:[a-f0-9]{64}$') {
        throw "Unsafe data-service configuration: $name"
    }
}

# Refuse stopped containers too: a later 'down' must only remove what this run created.
$existing = @(Invoke-Docker @('container', 'ls', '-aq', '--filter', $projectFilter))
if ($existing.Count) { throw 'sf-local containers already exist; leaving them untouched.' }
$networks = @(Invoke-Docker @('network', 'ls', '--filter', 'name=sf-local_default', '--format', '{{.Name}}') | Where-Object { $_ -ceq 'sf-local_default' })
if ($networks.Count) { throw 'sf-local_default already exists; inspect its owner before a verification run.' }
foreach ($volume in @('pgdata', 'redisdata')) {
    $name = "${project}_$volume"
    $exists = @(Invoke-Docker @('volume', 'ls', '--filter', "name=$name", '--format', '{{.Name}}') | Where-Object { $_ -ceq $name })
    if ($exists.Count) {
        $labels = (Invoke-Docker @('volume', 'inspect', $name, '--format', '{{json .Labels}}')) | ConvertFrom-Json
        if ($labels.'com.docker.compose.project' -ne $project -or $labels.'com.docker.compose.volume' -ne $volume) {
            throw "Unexpected owner of volume $name"
        }
    }
}
Write-Output 'Preflight OK: local engine, isolated sf-local configuration and no existing containers/network.'
if ($PreflightOnly) { return }

# The caller owns the exclusive resource window throughout startup and drain.
$failures = [Collections.Generic.List[string]]::new()
$started = $false
try {
    Invoke-Docker ($compose + @('up', '-d', '--wait', '--wait-timeout', '120'))
    $started = $true
    Invoke-Docker ($compose + @('exec', '-T', 'postgres', 'sh', '/opt/supportflow/check-postgres.sh'))
    $reply = (Invoke-Docker ($compose + @('exec', '-T', 'redis', 'redis-cli', '--raw', 'PING')) | Out-String).Trim()
    if ($reply -notmatch '^(\(error\)\s+)?NOAUTH\s+Authentication required\.$') { throw 'Unexpected unauthenticated Redis reply.' }
    $reply = (Invoke-Docker ($compose + @('exec', '-T', 'redis', 'sh', '-ec',
        'REDISCLI_AUTH=$(cat /run/secrets/redis_password) redis-cli --raw PING')) | Out-String).Trim()
    if ($reply -cne 'PONG') { throw 'Authenticated Redis PING did not return PONG.' }
    Write-Output 'OK Redis rejected unauthenticated PING and returned PONG after authentication.'
} catch {
    $failures.Add("Runtime verification: $($_.Exception.Message)")
} finally {
    try {
        Invoke-Docker ($compose + @('down', '--timeout', '15'))
    } catch {
        $failures.Add("Shutdown: $($_.Exception.Message)")
    }
    try {
        $remaining = @(Invoke-Docker @('container', 'ls', '-aq', '--filter', $projectFilter))
        $networks = @(Invoke-Docker @('network', 'ls', '-q', '--filter', $projectFilter))
        if ($remaining.Count -or $networks.Count) { throw 'sf-local containers or networks remain after down.' }
        if ($started) {
            $volumes = @(Invoke-Docker @('volume', 'ls', '--filter', $projectFilter, '--format', '{{.Name}}'))
            foreach ($expected in @('sf-local_pgdata', 'sf-local_redisdata')) {
                if ($expected -notin $volumes) { throw "Missing persistent volume after down: $expected" }
            }
        }
        Write-Output 'Drain OK: zero sf-local containers and networks.'
    } catch {
        $failures.Add("Drain verification: $($_.Exception.Message)")
    }
}
if ($failures.Count) { throw ($failures -join "`n") }
