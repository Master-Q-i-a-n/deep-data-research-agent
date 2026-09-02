[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$workspaceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "../..")).Path
$composePath = Join-Path $workspaceRoot "compose.redis.yaml"
$secretDirectory = Join-Path $workspaceRoot ".secrets"
$secretPath = Join-Path $secretDirectory "redis_password"
$containerName = "ddra-redis"
$serviceName = "ddra-redis"

New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null

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

$legacyId = (& docker ps -aq --filter "name=^/redis$" | Out-String).Trim()
if ($legacyId) {
    $composeProject = (docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' $legacyId 2>$null).Trim()
    $composeService = (docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' $legacyId 2>$null).Trim()
    if ($composeProject -eq "deep-data-research-agent" -and $composeService -eq "redis") {
        docker rm --force $legacyId | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "旧 Redis 容器移除失败"
        }
        docker volume inspect redis-data *> $null
        if ($LASTEXITCODE -eq 0) {
            docker volume rm redis-data | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "旧 Redis 数据卷移除失败"
            }
        }
    }
    else {
        Write-Host "[skip]    保留非本项目的 redis 容器"
    }
}

docker compose -f $composePath up -d --build --force-recreate $serviceName
if ($LASTEXITCODE -ne 0) {
    throw "Redis Compose 启动失败"
}

for ($attempt = 0; $attempt -lt 30; $attempt++) {
    $health = (docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' $containerName 2>$null).Trim()
    if ($health -eq "healthy") {
        break
    }
    Start-Sleep -Seconds 1
}
if ($health -ne "healthy") {
    docker logs --tail 80 $containerName
    throw "Redis 未在预期时间内进入 healthy 状态"
}

$unauthenticated = (docker exec $containerName redis-cli --no-auth-warning PING 2>&1 | Out-String).Trim()
if ($unauthenticated -notmatch "NOAUTH") {
    throw "Redis 未拒绝匿名访问"
}
$authenticated = (docker exec $containerName sh -c 'REDISCLI_AUTH="$(cat /run/secrets/redis_password)" redis-cli --user ddra --no-auth-warning PING').Trim()
if ($authenticated -ne "PONG") {
    throw "Redis ACL 认证验证失败"
}
$celeryAuthenticated = (docker exec $containerName sh -c 'REDISCLI_AUTH="$(cat /run/secrets/redis_password)" redis-cli --user ddra-celery --no-auth-warning -n 1 PING').Trim()
if ($celeryAuthenticated -ne "PONG") {
    throw "Celery Redis ACL 认证验证失败"
}

Write-Output "Redis 已由 Compose 管理，项目镜像、16379 端口和 ACL 验证通过。"
