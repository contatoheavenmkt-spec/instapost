# start-worker-forever.ps1
# Wrapper que mantem o worker rodando para sempre.
# Se o worker.py morrer por qualquer motivo (rede, erro, etc), reinicia em 10s.
#
# Uso:
#   1. Abre PowerShell (NAO precisa ser admin)
#   2. cd C:\Users\tutif\Desktop\insta-poster
#   3. .\start-worker-forever.ps1
#
# Pra parar: Ctrl+C 2 vezes (primeiro para o python, depois sai do loop)

$ErrorActionPreference = "Continue"
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Insta Poster Worker - MODO PERPETUO" -ForegroundColor Cyan
Write-Host "  (reinicia automatico se cair)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Ativa venv
if (-not (Test-Path ".\venv\Scripts\Activate.ps1")) {
    Write-Host "ERRO: venv nao encontrado em $ProjectRoot\venv" -ForegroundColor Red
    exit 1
}
. .\venv\Scripts\Activate.ps1

$RestartCount = 0
$StartTime = Get-Date

while ($true) {
    $iter_start = Get-Date

    Write-Host ""
    Write-Host "[$($iter_start.ToString('HH:mm:ss'))] Iniciando worker (execucao #$($RestartCount + 1))" -ForegroundColor Green
    Write-Host ""

    # Roda o worker (BLOQUEIA aqui ate ele morrer)
    try {
        python worker.py
        $ExitCode = $LASTEXITCODE
    } catch {
        Write-Host "Exception no wrapper: $_" -ForegroundColor Red
        $ExitCode = -1
    }

    $iter_end = Get-Date
    $ran_for = ($iter_end - $iter_start).TotalSeconds

    Write-Host ""
    Write-Host "[$($iter_end.ToString('HH:mm:ss'))] Worker saiu (exit=$ExitCode), rodou por $([int]$ran_for)s" -ForegroundColor Yellow

    $RestartCount++
    $total_uptime = ($iter_end - $StartTime).TotalHours
    Write-Host "    Total restarts: $RestartCount | Uptime do wrapper: $([math]::Round($total_uptime,2))h" -ForegroundColor DarkGray

    # Se o worker caiu rapido (< 30s), espera mais antes de reiniciar (evita loop infinito de erro)
    if ($ran_for -lt 30) {
        Write-Host "    Worker caiu em < 30s -- esperando 60s antes de tentar de novo" -ForegroundColor DarkYellow
        Start-Sleep -Seconds 60
    } else {
        Write-Host "    Esperando 10s e reiniciando..." -ForegroundColor DarkGray
        Start-Sleep -Seconds 10
    }
}
