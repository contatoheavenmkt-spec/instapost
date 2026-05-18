# sync-sessions.ps1
# Sobe todas as sessões Instagram do PC local pra VPS via SCP.
#
# Uso (no PowerShell, dentro da pasta insta-poster):
#   .\deploy\sync-sessions.ps1
#
# Edita as variáveis abaixo na primeira execução.

# === CONFIG ===
$VPS_IP   = "72.61.27.105"
$VPS_USER = "root"
$VPS_PATH = "/opt/instapost/data/sessions/"
$LOCAL_SESSIONS = "$PSScriptRoot\..\sessions"
# ==============

Write-Host ""
Write-Host "==> Sync de sessões Insta: PC local -> VPS" -ForegroundColor Cyan
Write-Host "    Local : $LOCAL_SESSIONS"
Write-Host "    VPS   : $VPS_USER@$VPS_IP`:$VPS_PATH"
Write-Host ""

# Lista os arquivos a enviar
$files = Get-ChildItem -Path $LOCAL_SESSIONS -Filter "*.json" -ErrorAction SilentlyContinue
if (-not $files) {
    Write-Host "Nenhuma sessão encontrada em $LOCAL_SESSIONS" -ForegroundColor Yellow
    Write-Host "Conecte as contas localmente primeiro (rode: python run.py)" -ForegroundColor Yellow
    exit 1
}

Write-Host "Sessões encontradas ($($files.Count)):" -ForegroundColor Green
$files | ForEach-Object { Write-Host "   - $($_.Name)" }
Write-Host ""

# Pergunta confirmação
$ans = Read-Host "Confirmar envio? (s/N)"
if ($ans -ne 's' -and $ans -ne 'S') {
    Write-Host "Cancelado." -ForegroundColor Yellow
    exit 0
}

# Envia tudo via scp (vai pedir senha SSH 1x)
& scp "$LOCAL_SESSIONS\*.json" "$VPS_USER@$VPS_IP`:$VPS_PATH"

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "OK! Sessões enviadas." -ForegroundColor Green
    Write-Host ""
    Write-Host "Agora no painel da VPS (https://instapost.shop):" -ForegroundColor Cyan
    Write-Host "  1. Confirma que as contas estao adicionadas (com mesma senha/2FA)"
    Write-Host "  2. NAO clica em Conectar (ja esta conectado via sessao copiada)"
    Write-Host "  3. Pode disparar postagem direto"
} else {
    Write-Host ""
    Write-Host "ERRO no SCP (codigo $LASTEXITCODE)" -ForegroundColor Red
    Write-Host "Verifica se a senha root da VPS esta correta."
}
