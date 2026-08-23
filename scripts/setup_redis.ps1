[CmdletBinding()]
param(
    [string]$ExpectedLegacyContainerId = "f10fedb99816"
)

$ErrorActionPreference = "Stop"
$workspaceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$composePath = Join-Path $workspaceRoot "compose.redis.yaml"
$secretDirectory = Join-Path $workspaceRoot ".secrets"
$secretPath = Join-Path $secretDirectory "redis_password"
$backupDirectory = Join-Path $workspaceRoot ".redis-backups"

New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null

if (-not (Test-Path -LiteralPath $secretPath -PathType Leaf)) {
    $secret = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
    [IO.File]::WriteAllText($secretPath, "$secret`n", [Text.UTF8Encoding]::new($false))
}

$secretLength = (Get-Content -LiteralPath $secretPath -Encoding UTF8 -Raw).Trim().Length
if ($secretLength -lt 32) {
    throw "Redis 密钥至少需要 32 个字符：$secretPath"
}

docker compose -f $composePath config --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Redis Compose 配置校验失败"
}

$legacyId = (docker ps -aq --filter "name=^/redis$").Trim()
if ($legacyId) {
    $composeService = (docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' $legacyId 2>$null).Trim()
    if ($composeService -eq "<no value>") {
        $composeService = ""
    }
    if ($composeService -and $composeService -ne "redis") {
        throw "redis 容器属于未知 Compose 服务 $composeService，未执行替换"
    }
    if (-not $composeService) {
        if (-not $legacyId.StartsWith($ExpectedLegacyContainerId, [StringComparison]::OrdinalIgnoreCase)) {
            throw "发现未知的 redis 容器 $legacyId，未执行替换"
        }

        $backupName = "redis-data-{0}.tar.gz" -f (Get-Date -Format "yyyyMMdd-HHmmss")
        docker run --rm --entrypoint sh `
            --volume "redis-data:/source:ro" `
            --volume "${backupDirectory}:/backup" `
            redis:7.4-alpine `
            -c "tar -czf /backup/$backupName -C /source ."
        if ($LASTEXITCODE -ne 0) {
            throw "Redis 数据卷备份失败，未停止旧容器"
        }

        docker stop $legacyId | Out-Null
        docker rm $legacyId | Out-Null
    }
}

docker compose -f $composePath up -d
if ($LASTEXITCODE -ne 0) {
    throw "Redis Compose 启动失败"
}

for ($attempt = 0; $attempt -lt 30; $attempt++) {
    $health = (docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' redis 2>$null).Trim()
    if ($health -eq "healthy") {
        break
    }
    Start-Sleep -Seconds 1
}
if ($health -ne "healthy") {
    docker logs --tail 80 redis
    throw "Redis 未在预期时间内进入 healthy 状态"
}

$unauthenticated = (docker exec redis redis-cli --no-auth-warning PING 2>&1 | Out-String).Trim()
if ($unauthenticated -notmatch "NOAUTH") {
    throw "Redis 未拒绝匿名访问"
}
$authenticated = (docker exec redis sh -c 'REDISCLI_AUTH="$(cat /run/secrets/redis_password)" redis-cli --user ddra --no-auth-warning PING').Trim()
if ($authenticated -ne "PONG") {
    throw "Redis ACL 认证验证失败"
}

Write-Output "Redis 已由 Compose 管理，数据卷 redis-data 已保留，ACL 与健康检查验证通过。"
