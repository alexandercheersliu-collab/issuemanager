#requires -Version 5.1
<#
.SYNOPSIS
    MathMaster Edu 一键环境初始化（Windows）。

.DESCRIPTION
    1. 创建 .venv 虚拟环境（若不存在）
    2. 安装 requirements.txt 依赖
    3. 复制 .env.example -> .env（若不存在）
    4. 调用 scripts/install_onnx_model.py 预下载 ChromaDB 内置嵌入模型（可用 -SkipModel 跳过）

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/setup_env.ps1
    powershell -ExecutionPolicy Bypass -File scripts/setup_env.ps1 -SkipModel
#>
[CmdletBinding()]
param(
    [switch]$SkipModel
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $ProjectRoot

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

# ---------- 1. 定位 Python ----------
Write-Step '检查 Python ...'
$Python = $null
foreach ($cmd in @('python', 'py')) {
    $found = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($found) { $Python = $cmd; break }
}
if (-not $Python) {
    Write-Error '未找到 Python，请先安装 Python 3.11+ 并加入 PATH。'
    exit 1
}
& $Python --version
if ($LASTEXITCODE -ne 0) { Write-Error 'Python 不可用。'; exit 1 }

# ---------- 2. 创建 venv ----------
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (Test-Path $VenvPython) {
    Write-Step '.venv 已存在，跳过创建。'
} else {
    Write-Step '创建虚拟环境 .venv ...'
    & $Python -m venv (Join-Path $ProjectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { Write-Error '创建虚拟环境失败。'; exit 1 }
}

# ---------- 3. 安装依赖 ----------
Write-Step '安装依赖 requirements.txt ...'
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Write-Error '升级 pip 失败。'; exit 1 }
& $VenvPython -m pip install -r (Join-Path $ProjectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Write-Error '依赖安装失败。'; exit 1 }

# ---------- 4. 准备 .env ----------
$EnvFile = Join-Path $ProjectRoot '.env'
$EnvExample = Join-Path $ProjectRoot '.env.example'
if (Test-Path $EnvFile) {
    Write-Step '.env 已存在，跳过复制。'
} elseif (Test-Path $EnvExample) {
    Write-Step '复制 .env.example -> .env ...'
    Copy-Item $EnvExample $EnvFile
    Write-Host '    请按需编辑 .env（AI_PROVIDER / AI_API_KEY 等）；未配置 Key 时以 mock 演示模式运行。'
} else {
    Write-Host '    警告：未找到 .env.example，跳过。' -ForegroundColor Yellow
}

# ---------- 5. 预下载嵌入模型 ----------
if ($SkipModel) {
    Write-Step '按参数要求跳过嵌入模型预下载（首次启动时 ChromaDB 会自行下载）。'
} else {
    Write-Step '预下载 ChromaDB 内置嵌入模型（all-MiniLM-L6-v2 ONNX，约 79MB）...'
    & $VenvPython (Join-Path $ProjectRoot 'scripts\install_onnx_model.py')
    if ($LASTEXITCODE -ne 0) {
        Write-Host '    模型预下载失败（网络问题可稍后重试）：' -ForegroundColor Yellow
        Write-Host '    .venv\Scripts\python.exe scripts\install_onnx_model.py' -ForegroundColor Yellow
    }
}

Write-Step '环境初始化完成！'
Write-Host ''
Write-Host '启动应用（二选一）：' -ForegroundColor Green
Write-Host '  Streamlit 界面:  .venv\Scripts\python.exe -m streamlit run app.py'
Write-Host '  FastAPI 网关:    .venv\Scripts\python.exe -m uvicorn api.main:app --port 8000'
