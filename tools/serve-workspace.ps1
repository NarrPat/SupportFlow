# Interactive, time-bounded demonstration using the persistent sf_app database.
[CmdletBinding()]
param(
    [ValidateRange(1,180)][int]$Minutes = 15,
    [switch]$WithWorker, [switch]$RequireIdle, [switch]$WithKnowledge, [switch]$WithClarification,
    [switch]$WithProposal, [switch]$WithApproval, [switch]$WithExecution, [switch]$WithDemoData,
    [switch]$ObserveExistingUpgrade, [switch]$WithGitHubIssues
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:SF_WEB_ACCOUNTS -or $env:SF_WEB_APP_DB -or $env:SF_WEB_SANDBOX_DB) {
    throw 'Unset the evaluation database/account overrides for the persistent demo.'
}
if ($WithKnowledge -and !$WithWorker) { throw 'WithKnowledge requires WithWorker.' }
if ($WithClarification -and !$WithKnowledge) { throw 'WithClarification requires WithKnowledge and WithWorker.' }
if ($WithProposal -and !$WithKnowledge) { throw 'WithProposal requires WithKnowledge and WithWorker.' }
if ($WithProposal -and $WithClarification) { throw 'Select either WithProposal or WithClarification, not both graph profiles.' }
if ($WithExecution -and !$WithApproval) { throw 'WithExecution requires WithApproval, WithProposal, WithKnowledge and WithWorker.' }
if ($WithApproval -and !$WithProposal) { throw 'WithApproval requires WithProposal, WithKnowledge and WithWorker.' }
if ($WithDemoData -and !$WithWorker) { throw 'WithDemoData requires WithWorker.' }
if ($WithGitHubIssues -and !$WithKnowledge) { throw 'WithGitHubIssues requires WithKnowledge and WithWorker.' }
$root = (Get-Item (Join-Path $PSScriptRoot '..')).FullName
$deploy = Join-Path $root 'deploy/supportflow'
$compose = @('compose','-p','sf-local','--project-directory',$deploy,'-f',"$deploy/compose.yaml",'-f',"$deploy/compose.web.yaml")
if ($WithWorker) { $compose += @('-f',"$deploy/compose.worker.yaml",'-f',"$deploy/compose.diagnosis.yaml") }
if ($WithKnowledge) { $compose += @('-f',"$deploy/compose.rag.yaml") }
if ($WithClarification) { $compose += @('-f',"$deploy/compose.clarification.yaml") }
if ($WithProposal) { $compose += @('-f',"$deploy/compose.proposal.yaml") }
if ($WithApproval) { $compose += @('-f',"$deploy/compose.approval.yaml") }
if ($WithExecution) { $compose += @('-f',"$deploy/compose.execution.yaml") }
if ($WithDemoData) { $compose += @('-f',"$deploy/compose.demodata.yaml") }
if ($WithGitHubIssues) {
    if ([string]::IsNullOrWhiteSpace($env:SF_GITHUB_ALLOWED_REPOSITORIES)) {
        throw 'WithGitHubIssues requires SF_GITHUB_ALLOWED_REPOSITORIES.'
    }
    if (!(Test-Path -LiteralPath "$deploy/.secrets/github_token" -PathType Leaf)) {
        throw 'WithGitHubIssues requires the local github_token credential file.'
    }
    $compose += @('-f',"$deploy/compose.github.yaml")
}
$filter = 'label=com.docker.compose.project=sf-local'
$result = @{failure = $null}

function Invoke-Compose([string[]]$Arguments) {
    & docker @compose @Arguments
    if ($LASTEXITCODE) { throw "Compose command failed: $($Arguments[0])" }
}
function Observe-ProposalPhase([string]$Phase) {
    if (!$WithProposal -or !$RequireIdle) { return }
    Invoke-Compose @('run','--rm','--no-deps','-T','app-setup','python','-B','-m','apps.supportflow.db.proposal_startup_probe','--target','app','--phase',$Phase)
    Invoke-Compose @('run','--rm','--no-deps','-T','sandbox-setup','python','-B','-m','apps.supportflow.db.proposal_startup_probe','--target','sandbox','--phase',$Phase)
    if ($Phase -in @('live','final')) {
        # Execute the exact committed probe inside the actual API container,
        # without mounting a database directory or a migrator secret there.
        $probeSource = [IO.File]::ReadAllText("$root/apps/supportflow/db/proposal_startup_probe.py")
        $probeSource | & docker @compose exec -T api python -B - --target api --phase $Phase
        if ($LASTEXITCODE) { throw 'Proposal API configuration observation failed.' }
    }
}
function Show-Workspace {
    # Only down after preflight has established exclusive ownership of the stack.
    try {
        & "$deploy/verify-runtime.ps1" -PreflightOnly
        $networks = @(docker network ls -q --filter $filter)
        if ($LASTEXITCODE -or $networks.Count) { throw 'The demo requires a clean sf-local network boundary.' }
        if ($WithWorker -and !(Test-Path -LiteralPath "$deploy/.secrets/deepseek_api_key" -PathType Leaf)) {
            throw 'Worker profile requires the local deepseek_api_key credential file.'
        }
        & "$root/.venv/Scripts/python.exe" -B "$deploy/init_accounts.py"
        if ($LASTEXITCODE) { throw 'Demo credential setup failed.' }
        Push-Location "$root/apps/supportflow/web"
        try {
            & npm.cmd run build:product
            if ($LASTEXITCODE) { throw 'Product build failed.' }
        } finally { Pop-Location }
    } catch { $result.failure = $_; return }
    try {
        Invoke-Compose @('up','--no-deps','-d','--wait','--wait-timeout','120','postgres')
        if ($ObserveExistingUpgrade) { Observe-ProposalPhase 'before_migration' }
        Invoke-Compose @('run','--rm','--no-deps','-T','app-setup')
        if ($RequireIdle) { Invoke-Compose @('run','--rm','--no-deps','-T','app-setup','python','-B','-m','apps.supportflow.db.demo_state','--require-idle') }
        if ($WithWorker) { Invoke-Compose @('run','--rm','--no-deps','-T','sandbox-setup') }
        if ($WithDemoData) { Invoke-Compose @('run','--rm','--no-deps','-T','demo-data-setup') }
        Observe-ProposalPhase 'after_migration'
        if ($WithKnowledge) { Invoke-Compose @('run','--rm','--no-deps','-T','knowledge-setup') }
        Observe-ProposalPhase 'after_ensure'
        Invoke-Compose @('up','-d','--wait','--wait-timeout','100','web')
        if ($WithWorker) { Invoke-Compose @('up','-d','--wait','--wait-timeout','100','sandbox','worker','dispatcher','scanner') }
        $page = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18080/tickets' -TimeoutSec 10
        $bootstrap = Invoke-RestMethod -UseBasicParsing -Uri 'http://127.0.0.1:18080/api/v1/auth/csrf' -TimeoutSec 10
        if ($page.StatusCode -ne 200 -or $page.Content -notmatch '/assets/' -or !$bootstrap.csrf_token) {
            throw 'Gateway did not return the built application and anonymous bootstrap.'
        }
        Write-Host 'Gateway verified: ticket SPA and anonymous API bootstrap.'
        Observe-ProposalPhase 'live'
        Write-Host "Open http://127.0.0.1:18080/tickets . Accounts are in $deploy/.secrets/seed-accounts.json ."
        Write-Host "Demo duration: $Minutes minute(s). The stack drains automatically."
        if ($WithExecution) { Write-Host 'Execution mode: supervisor approval is followed by one temporary-seat grant, receipt verification and effect-aware recovery. Old snapshots retain their original graph.' }
        elseif ($WithApproval) { Write-Host 'Approval mode: decisions and dispatch authorization only; no business grant transport. Authorized runs explicitly hand off.' }
        elseif ($WithProposal) { Write-Host 'Proposal mode: prepares immutable pending review only; this legacy graph does not accept decisions or business grants.' }
        if ($WithDemoData) { Write-Host 'Demo invitation data: tenant_a customer C-001 and member@example.com. Existing grants and original invitation dates are retained.' }
        Start-Sleep -Seconds ($Minutes * 60)
        Observe-ProposalPhase 'final'
        if ($RequireIdle) { Invoke-Compose @('run','--rm','--no-deps','-T','app-setup','python','-B','-m','apps.supportflow.db.demo_state','--require-idle') }
        if ($WithProposal -and $RequireIdle) {
            # Keep only the documented nonsecret structured scanner observation.
            $scannerLines = @(& docker @compose logs --no-color --no-log-prefix scanner 2>&1)
            if ($LASTEXITCODE) { throw 'Retention scheduler log observation failed.' }
            foreach ($line in $scannerLines) {
                try { $tick = "$line" | ConvertFrom-Json -ErrorAction Stop } catch { continue }
                if ($null -ne $tick -and $null -ne $tick.PSObject.Properties['kind'] -and $tick.kind -eq 'event_retention') {
                    $projection = @{cutoff=$tick.cutoff; deleted=$tick.deleted} | ConvertTo-Json -Compress
                    Write-Output "SF_RETENTION_TICK $projection"
                }
            }
        }
    } catch { $result.failure = $_ }
    finally {
        Invoke-Compose @('down','--timeout','15')
        $containers = @(docker ps -aq --filter $filter)
        if ($LASTEXITCODE -or $containers.Count) { throw 'Demo container drain unconfirmed.' }
        $networks = @(docker network ls -q --filter $filter)
        if ($LASTEXITCODE -or $networks.Count) { throw 'Demo network drain unconfirmed.' }
        Write-Host 'Demo drained: containers and networks removed; data volumes retained.'
    }
}
if (Test-Path -LiteralPath 'F:\work_agent_proj') {
    & "$PSScriptRoot/with-runtime-window.ps1" -CoordinationDirectory 'F:\work_agent_proj\coordination' `
        -Purpose 'Time-limited SupportFlow ticket workspace' -Resources @('sf-local/18080 web, API, PostgreSQL; npm build', "Worker profile=$WithWorker; when enabled, Redis/Celery/DeepSeek with per-run 90s/8 calls/512 output cap", "Knowledge profile=$WithKnowledge; pinned local CPU embeddings and persistent knowledge volume", "Proposal profile=$WithProposal; pending review only, no approval or business grant") `
        -MaxMinutes ($Minutes+15) -Work { Show-Workspace }
} else { Show-Workspace }
if ($result.failure) { throw $result.failure }
