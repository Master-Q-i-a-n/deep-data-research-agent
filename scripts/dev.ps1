[CmdletBinding()]
param(
    [string]$SandboxDistro = "Debian",
    [string]$SandboxConfig = "~/.sandbox.toml",
    [switch]$SkipRedis,
    [switch]$SkipFrontend,
    [switch]$UseOssWorkspace
)

$ErrorActionPreference = "Stop"
$workspaceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$logDirectory = Join-Path ([IO.Path]::GetTempPath()) "deep-data-research-agent/dev-logs"
$startedProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()

function Resolve-Executable {
    param([Parameter(Mandatory)][string]$Name)

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "Command '$Name' was not found. Install it and make sure it is available on PATH."
    }
    return $command.Source
}

function Start-DevProcess {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$WorkingDirectory
    )

    $stdoutPath = Join-Path $logDirectory "$Name.log"
    $stderrPath = Join-Path $logDirectory "$Name.error.log"
    $process = Start-Process `
        -FilePath $Executable `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -WindowStyle Hidden `
        -PassThru
    $startedProcesses.Add($process)
    Write-Host ("[started] {0,-16} PID={1}  log={2}" -f $Name, $process.Id, $stdoutPath)
}

function Stop-DevProcessTree {
    param([Parameter(Mandatory)][System.Diagnostics.Process]$Process)

    if ($Process.HasExited) {
        return
    }

    # Stop only PIDs recorded by this script and their child processes.
    if ($IsWindows -or $env:OS -eq "Windows_NT") {
        & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null
    }
    else {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    }
}

function Assert-DevPortAvailable {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][int]$Port
    )

    $listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($listeners) {
        $processIds = ($listeners | Select-Object -ExpandProperty OwningProcess -Unique) -join ","
        throw "$Name cannot start because port $Port is already in use by PID $processIds. Stop the existing process and retry."
    }
}

function Wait-DevPort {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][int]$Port,
        [int]$TimeoutSeconds = 90
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        foreach ($process in $startedProcesses) {
            if ($process.HasExited) {
                throw "Development process PID=$($process.Id) exited with code $($process.ExitCode). Check $logDirectory."
            }
        }

        $client = [Net.Sockets.TcpClient]::new()
        try {
            $connection = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
            if ($connection.AsyncWaitHandle.WaitOne(500)) {
                $client.EndConnect($connection)
                Write-Host ("[ready]   {0,-16} port={1}" -f $Name, $Port)
                return
            }
        }
        catch {
            # The service is still starting; retry until the deadline.
        }
        finally {
            $client.Dispose()
        }
        Start-Sleep -Milliseconds 250
    }
    throw "$Name did not listen on port $Port within $TimeoutSeconds seconds. Check $logDirectory."
}

$uv = Resolve-Executable "uv"
$wsl = Resolve-Executable "wsl.exe"
$docker = $null
if (-not $SkipRedis) {
    $docker = Resolve-Executable "docker"
}
$npm = $null
$node = $null
$viteEntry = $null
if (-not $SkipFrontend) {
    $npmCommand = if ($IsWindows -or $env:OS -eq "Windows_NT") { "npm.cmd" } else { "npm" }
    $npm = Resolve-Executable $npmCommand
    $node = Resolve-Executable "node"
}

$envPath = Join-Path $workspaceRoot ".env"
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw "Missing $envPath. Create it from .env.example and fill in the real settings."
}

if ($SandboxDistro -notmatch '^[A-Za-z0-9_.-]+$') {
    throw "SandboxDistro contains unsupported characters: $SandboxDistro"
}
if ($SandboxConfig -notmatch '^[A-Za-z0-9_./~:-]+$') {
    throw "SandboxConfig contains unsupported characters: $SandboxConfig"
}
& $wsl -d $SandboxDistro -- bash -lc "command -v uvx >/dev/null 2>&1"
if ($LASTEXITCODE -ne 0) {
    throw "uvx was not found in WSL distribution '$SandboxDistro'."
}
& $wsl -d $SandboxDistro -- bash -lc "test -f $SandboxConfig"
if ($LASTEXITCODE -ne 0) {
    throw "OpenSandbox configuration was not found in WSL '$SandboxDistro': $SandboxConfig"
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$env:PYTHONUTF8 = "1"
# The development launcher owns these two mode switches. Explicit environment
# variables override values left in .env and are inherited by every child process.
$env:APP_ENV = "development"
$env:WORKSPACE_STORAGE_BACKEND = if ($UseOssWorkspace) { "oss" } else { "local" }
Write-Host ("[config]  Workspace storage: {0}" -f $env:WORKSPACE_STORAGE_BACKEND)

if (-not $SkipRedis) {
    $redisHealth = (& $docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' ddra-redis 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $redisHealth -ne "healthy") {
        Write-Host "Redis is missing or unhealthy. Running the Compose setup..."
        & (Join-Path $workspaceRoot "scripts/setup/redis.ps1")
    }
    else {
        Write-Host "[ready]   Redis"
    }
}

if (-not $SkipFrontend) {
    $frontendDirectory = Join-Path $workspaceRoot "frontend"
    $frontendEnv = Join-Path $frontendDirectory ".env.local"
    if (-not (Test-Path -LiteralPath $frontendEnv -PathType Leaf)) {
        Copy-Item -LiteralPath (Join-Path $frontendDirectory ".env.example") -Destination $frontendEnv
        Write-Host "Created frontend/.env.local from frontend/.env.example."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $frontendDirectory "node_modules") -PathType Container)) {
        Write-Host "Installing frontend dependencies for the first run..."
        & $npm ci --prefix $frontendDirectory
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend dependency installation failed."
        }
    }
    $viteEntry = Join-Path $frontendDirectory "node_modules/vite/bin/vite.js"
    if (-not (Test-Path -LiteralPath $viteEntry -PathType Leaf)) {
        throw "Vite entry point was not found: $viteEntry"
    }
}

$databaseCheck = "import asyncio, selectors; from deep_data_research_agent.database.repository import check_database_ready; asyncio.run(asyncio.wait_for(check_database_ready(), timeout=8), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))"
Write-Host "Checking the deployed PostgreSQL schema..."
$previousErrorActionPreference = $ErrorActionPreference
try {
    # Windows PowerShell 5.1 turns native stderr into an ErrorRecord when Stop is active.
    $ErrorActionPreference = "Continue"
    & $uv run python -c $databaseCheck 2>$null
    $databaseCheckExitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
}
if ($databaseCheckExitCode -ne 0) {
    throw "PostgreSQL schema is not ready. Configure .env.postgres-admin, run 'uv run setup-agent-postgres', and retry."
}
Write-Host "[ready]   PostgreSQL schema"

Assert-DevPortAvailable -Name "OpenSandbox" -Port 8080
Assert-DevPortAvailable -Name "LangGraph" -Port 2024
if (-not $SkipFrontend) {
    Assert-DevPortAvailable -Name "Frontend" -Port 5174
}

try {
    $sandboxCommand = "exec uvx opensandbox-server --config $SandboxConfig"
    Start-DevProcess `
        -Name "opensandbox" `
        -Executable $wsl `
        -Arguments @("-d", $SandboxDistro, "--", "bash", "-lc", ('"{0}"' -f $sandboxCommand)) `
        -WorkingDirectory $workspaceRoot
    Wait-DevPort -Name "OpenSandbox" -Port 8080 -TimeoutSeconds 60

    Start-DevProcess `
        -Name "langgraph" `
        -Executable $uv `
        -Arguments @("run", "python", "scripts/run_langgraph_dev.py") `
        -WorkingDirectory $workspaceRoot
    Wait-DevPort -Name "LangGraph" -Port 2024

    Start-DevProcess `
        -Name "celery-worker" `
        -Executable $uv `
        -Arguments @(
            "run", "celery", "-A", "deep_data_research_agent.workers.app:celery_app",
            "worker", "--pool=solo", "-Q", "memory,mail,maintenance", "--loglevel=INFO"
        ) `
        -WorkingDirectory $workspaceRoot
    Start-DevProcess `
        -Name "celery-beat" `
        -Executable $uv `
        -Arguments @(
            "run", "celery", "-A", "deep_data_research_agent.workers.app:celery_app",
            "beat", "--loglevel=INFO", "--schedule", "data/celerybeat-schedule"
        ) `
        -WorkingDirectory $workspaceRoot

    if (-not $SkipFrontend) {
        Start-DevProcess `
            -Name "frontend" `
            -Executable $node `
            -Arguments @(('"{0}"' -f $viteEntry)) `
            -WorkingDirectory (Join-Path $workspaceRoot "frontend")
        Wait-DevPort -Name "Frontend" -Port 5174 -TimeoutSeconds 60
    }

    Write-Host ""
    Write-Host "Development services are ready:"
    Write-Host "  LangGraph:   http://127.0.0.1:2024"
    if (-not $SkipFrontend) {
        Write-Host "  Frontend:    http://127.0.0.1:5174"
    }
    Write-Host "  OpenSandbox: http://127.0.0.1:8080"
    Write-Host "  Logs:        $logDirectory"
    Write-Host "Press Ctrl+C to stop these processes. The Redis container will remain running."

    while ($true) {
        Start-Sleep -Seconds 1
        foreach ($process in $startedProcesses) {
            if ($process.HasExited) {
                throw "Development process PID=$($process.Id) exited with code $($process.ExitCode). Check $logDirectory."
            }
        }
    }
}
finally {
    Write-Host "`nStopping development processes..."
    foreach ($process in $startedProcesses) {
        Stop-DevProcessTree $process
    }
}
