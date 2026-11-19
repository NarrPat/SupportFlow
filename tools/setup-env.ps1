[CmdletBinding()]
param(
    [ValidateSet('Prepare', 'Install')]
    [string] $Stage = 'Prepare',
    [string] $Python312
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$uvExe = Join-Path $projectRoot '.tools/uv-0.13.7/bin/uv.exe'
$venvPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
$pin = (Get-Content -Raw (Join-Path $projectRoot '.python-version')).Trim()
if ($Python312) { $Python312 = (Resolve-Path -LiteralPath $Python312).Path }

function Invoke-Checked([string] $File, [string[]] $Arguments) {
    if ($windowDeadline -and [DateTimeOffset]::UtcNow -ge $windowDeadline) {
        throw 'Exclusive window has reached its deadline.'
    }
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$File $($Arguments -join ' ') exited $LASTEXITCODE" }
}

function Prepare-Environment {
    if (-not (Test-Path $uvExe)) {
        Invoke-Checked python @('-m', 'pip', '--isolated', '--disable-pip-version-check', 'install',
            '--index-url', 'https://pypi.org/simple', '--no-compile', '--no-deps', '--only-binary=:all:',
            '--require-hashes', '-r', 'tools/bootstrap-uv.windows.txt', '--target', '.tools/uv-0.13.7',
            '--cache-dir', '.cache/pip')
    }
    $uvVersion = & $uvExe --version
    if ($LASTEXITCODE -ne 0 -or $uvVersion -notmatch '^uv 0\.13\.7\b') { throw 'Unexpected uv version' }
    if (-not (Test-Path $venvPython)) {
        if (Test-Path '.venv') { throw 'Existing .venv is incomplete. Inspect it before retrying.' }
        if ($Python312) {
            $version = & $Python312 -I -B -c 'import platform; print(platform.python_version())'
            if ($LASTEXITCODE -ne 0 -or $version -ne $pin) { throw "Supply a Python $pin interpreter" }
            Invoke-Checked $uvExe @('venv', '--no-python-downloads', '--python', $Python312, '.venv')
        } elseif ($Stage -eq 'Prepare') {
            Invoke-Checked $uvExe @('venv', '--no-python-downloads', '--python', $pin, '.venv')
        } else {
            Invoke-Checked $uvExe @('venv', '--managed-python', '--python', $pin, '.venv')
        }
    }
    Invoke-Checked $venvPython @('-I', '-B', 'tools/check_python_env.py', '--interpreter-only')
    Invoke-Checked $uvExe @('lock', '--check', '--no-build', '--no-python-downloads', '--python', $venvPython)
    Invoke-Checked $venvPython @('-I', '-B', 'deploy/supportflow/init_secrets.py')
    Invoke-Checked docker @('compose', '-p', 'sf-local', '-f', 'deploy/supportflow/compose.yaml', 'config', '--quiet')
}

function Install-Environment {
    Prepare-Environment
    & "$projectRoot/deploy/supportflow/verify-runtime.ps1" -PreflightOnly
    Invoke-Checked $uvExe @('sync', '--locked', '--no-python-downloads', '--python', $venvPython)
    Invoke-Checked $uvExe @('pip', 'check', '--python', $venvPython)
    Invoke-Checked $venvPython @('-I', '-B', 'tools/check_python_env.py')
    Push-Location 'apps/supportflow/web'
    try {
        Invoke-Checked npm.cmd @('ci', '--no-fund', '--no-audit')
        Invoke-Checked npm.cmd @('run', 'build')
    } finally { Pop-Location }
    Invoke-Checked docker @('build', '--platform', 'linux/amd64', '-t', 'sf-local-python-dev:env',
        '-f', 'deploy/supportflow/Dockerfile.python-dev', '.')
    Invoke-Checked docker @('run', '--rm', '--network', 'none', 'sf-local-python-dev:env',
        'python', '-I', '-B', 'tools/check_python_env.py')
    if ($windowDeadline -and [DateTimeOffset]::UtcNow -ge $windowDeadline) {
        throw 'Window elapsed before the database runtime check.'
    }
    & "$projectRoot/deploy/supportflow/verify-runtime.ps1"
}

# Limit uv state to this checkout even when the caller uses a shared host or Conda.
$settings = @{
    UV_CACHE_DIR = "$projectRoot/.cache/uv"
    UV_PYTHON_INSTALL_DIR = "$projectRoot/.tools/python"
    UV_PYTHON_BIN_DIR = "$projectRoot/.tools/bin"
    UV_PROJECT_ENVIRONMENT = "$projectRoot/.venv"
    UV_CONCURRENT_DOWNLOADS = '2'
    UV_CONCURRENT_BUILDS = '1'
    UV_CONCURRENT_INSTALLS = '1'
}
$previous = @{}
Push-Location $projectRoot
try {
    foreach ($key in $settings.Keys) {
        $previous[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $settings[$key], 'Process')
    }
    if ($Stage -eq 'Prepare') {
        Prepare-Environment
    } elseif (Test-Path -LiteralPath 'F:\work_agent_proj') {
        & "$PSScriptRoot/with-runtime-window.ps1" -CoordinationDirectory 'F:\work_agent_proj\coordination' `
            -Resources @('project .venv', 'web/node_modules', 'sf-local-python-dev:env', 'sf-local postgres and redis') `
            -Purpose 'SF-I001 isolated dependency install and development environment smoke' `
            -Work { param($windowDeadline); Install-Environment }
    } else {
        Install-Environment
    }
} finally {
    foreach ($key in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previous[$key], 'Process')
    }
    Pop-Location
}
