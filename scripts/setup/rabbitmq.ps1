[CmdletBinding()]
param(
    [string]$ContainerName = "rabbitmq",
    [string]$VHost = "ddra",
    [string]$User = "guest"
)

$ErrorActionPreference = "Stop"
$workspaceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "../..")).Path

# Provision only this project's namespace in an existing container. Existing
# credentials, other vhosts, queues and container settings remain owned by the user.
$vhosts = @(docker exec $ContainerName rabbitmqctl -q list_vhosts name)
if ($LASTEXITCODE -ne 0) {
    throw "无法访问现有 RabbitMQ 容器：$ContainerName"
}
if ($VHost -notin $vhosts) {
    docker exec $ContainerName rabbitmqctl add_vhost $VHost
    if ($LASTEXITCODE -ne 0) {
        throw "RabbitMQ vhost 创建失败：$VHost"
    }
}
docker exec $ContainerName rabbitmqctl set_permissions -p $VHost $User ".*" ".*" ".*"
if ($LASTEXITCODE -ne 0) {
    throw "RabbitMQ vhost 授权失败：$VHost"
}

Push-Location $workspaceRoot
try {
    # The application's URL controls the target used to declare/check queues.
    # Suppress native stderr so connection failures never print a credential URL.
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $env:PYTHONUTF8 = "1"
    & uv run python -c "from deep_data_research_agent.workers.app import declare_task_queues; declare_task_queues()" 2>$null
    $checkExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorActionPreference
    if ($checkExitCode -ne 0) {
        throw "AMQP 连接或队列初始化失败，请确认 CELERY_BROKER_URL 指向已授权的 vhost。"
    }
}
finally {
    $ErrorActionPreference = "Stop"
    Pop-Location
}
Write-Output "RabbitMQ 已就绪：复用 $ContainerName，vhost=$VHost，三个业务队列及三个死信队列。"
