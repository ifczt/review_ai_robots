$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "未找到虚拟环境 Python：$python"
}

Set-Location $root

$pyinstallerInstalled = $true
try {
    & $python -m pip show pyinstaller *> $null
    if ($LASTEXITCODE -ne 0) {
        $pyinstallerInstalled = $false
    }
} catch {
    $pyinstallerInstalled = $false
}

if (-not $pyinstallerInstalled) {
    Write-Host "正在安装 PyInstaller..."
    & $python -m pip install pyinstaller
}

Write-Host "开始构建 bot.exe ..."
& $python -m PyInstaller --noconfirm --clean bot.spec

$runtimeFiles = @(
    ".env",
    ".env.example",
    "db_connections.toml.example",
    "db_connections.toml",
    "ssh_servers.toml.example",
    "ssh_servers.toml",
    "freeze_state.json",
    "IFCZT_KEYS"
)

foreach ($item in $runtimeFiles) {
    $source = Join-Path $root $item
    if (Test-Path $source) {
        Copy-Item $source (Join-Path $root "dist") -Force
    }
}

$dataDir = Join-Path $root "data"
if (Test-Path $dataDir) {
    $targetDataDir = Join-Path $root "dist\data"
    if (Test-Path $targetDataDir) {
        Remove-Item $targetDataDir -Recurse -Force
    }
    Copy-Item $dataDir $targetDataDir -Recurse -Force
}

Write-Host ""
Write-Host "构建完成。"
Write-Host "可执行文件: $root\dist\bot.exe"
Write-Host "运行所需配置文件也已复制到 dist 目录。"
