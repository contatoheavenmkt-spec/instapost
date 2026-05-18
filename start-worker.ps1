# start-worker.ps1
# Atualiza dependencias e inicia o worker.
#
# Como usar:
#   1. Botao direito > Executar com PowerShell
#   OU
#   2. No PowerShell: cd C:\Users\tutif\Desktop\insta-poster; .\start-worker.ps1

$ErrorActionPreference = "Continue"
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Insta Poster Worker - inicializando" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 1. Ativa venv
Write-Host "[1/4] Ativando venv..." -ForegroundColor Yellow
if (-not (Test-Path ".\venv\Scripts\Activate.ps1")) {
    Write-Host "ERRO: venv nao encontrado em $ProjectRoot\venv" -ForegroundColor Red
    Write-Host "Cria com: python -m venv venv; .\venv\Scripts\pip install -r requirements.txt" -ForegroundColor Red
    exit 1
}
. .\venv\Scripts\Activate.ps1

# 2. Git pull (tolera falha de conexao)
Write-Host ""
Write-Host "[2/4] Atualizando codigo (git pull)..." -ForegroundColor Yellow
try {
    git pull 2>&1 | Out-Host
} catch {
    Write-Host "AVISO: git pull falhou (sem internet?), seguindo com codigo local" -ForegroundColor Yellow
}

# 3. Upgrade instagrapi
Write-Host ""
Write-Host "[3/4] Atualizando dependencia instagrapi..." -ForegroundColor Yellow
pip install --upgrade --quiet instagrapi 2>&1 | Out-Host

# 4. Inicia worker
Write-Host ""
Write-Host "[4/4] Iniciando worker (Ctrl+C para parar)..." -ForegroundColor Green
Write-Host ""
python worker.py
