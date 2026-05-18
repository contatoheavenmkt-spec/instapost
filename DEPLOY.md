# Deploy em VPS

Guia prático pra subir o Insta Poster. Há dois modos:

- **Modo A — VPS limpa** (Caddy embutido no compose, HTTPS automático)
- **Modo B — VPS com Nginx já rodando** (proxy via Nginx existente do host) ← se você já tem outros sites na mesma máquina

Os dois modos compartilham as etapas 1-2 (preparar VPS + DNS).

---

## 1. Contratar e preparar a VPS

1. Em `hostinger.com.br/vps-hosting`, escolhe um plano **KVM 1** (mínimo) ou **KVM 2** (recomendado pra upload de Reels grandes). DC: **São Paulo**.
2. Sistema operacional: **Ubuntu 24.04 LTS**.
3. Anota: **IP da VPS** + **senha root** (chega no email).

Você pode pedir pra eles deixar o Docker pré-instalado no provisioning — economiza 1 etapa.

---

## 2. Apontar o domínio pra VPS

No painel onde você comprou o domínio (Registro.br, GoDaddy, Hostinger, etc), vai em **DNS / Zona DNS** e cria um registro:

| Tipo | Nome | Valor | TTL |
|------|------|-------|-----|
| A | `insta` (ou `@` se for raiz) | `IP_DA_VPS` | 300 |

Espera ~5-15 min e testa no terminal:
```bash
nslookup insta.seudominio.com.br
```
Tem que retornar o IP da VPS.

---

## 3. Instalar Docker (1ª vez)

Conecta na VPS:
```bash
ssh root@IP_DA_VPS
```

Se Docker ainda não tá instalado:
```bash
apt update && apt install -y docker.io docker-compose-plugin git
systemctl enable --now docker
```

Verifica:
```bash
docker --version
docker compose version
```

---

## 4. Clonar o projeto

Recomendo subir o código pro GitHub primeiro (privado), e clonar via SSH. Caso queira subir direto:

```bash
mkdir -p /opt && cd /opt
# opção A: clonar do GitHub privado (precisa SSH key configurada)
git clone git@github.com:SEU_USER/insta-poster.git
cd insta-poster

# opção B: upload via SCP do seu PC (rode no SEU PC, não na VPS)
# scp -r C:\Users\tutif\Desktop\insta-poster root@IP_DA_VPS:/opt/
```

---

## 5. Configurar variáveis

```bash
cd /opt/insta-poster
cp .env.example .env
nano .env
```

Preenche:
```env
DOMAIN=insta.seudominio.com.br
EMAIL=voce@email.com
PUBLIC_BASE_URL=https://insta.seudominio.com.br
```

Salva (`Ctrl+O` `Enter` `Ctrl+X`).

---

## 6. Subir os containers

```bash
docker compose up -d --build
```

Primeira vez demora ~3-5 min (baixa Python + ffmpeg + monta imagem).

Confere se subiu:
```bash
docker compose ps
docker compose logs app | tail -30
docker compose logs caddy | tail -20
```

Esperado nos logs do Caddy: `certificate obtained successfully`. Pode demorar 30-60s.

---

## 7. Primeiro acesso

Abre no navegador: **`https://insta.seudominio.com.br`**

- Login: `edson.juan.oliversilva@gmail.com`
- Senha: `@Tos1725`

**TROCA ESSA SENHA depois!** (Fase 4 ainda não tem UI de trocar senha — por enquanto edita via terminal, instruções no fim deste arquivo.)

---

## 8. Operação diária

### Ver logs em tempo real
```bash
docker compose logs -f app
```

### Atualizar o código depois de mudanças
```bash
cd /opt/insta-poster
git pull                                # se vier do git
docker compose up -d --build            # rebuild só o que mudou
```

### Backup dos dados
Tudo importante está em `/opt/insta-poster/data/`:
```bash
tar czf backup-$(date +%F).tar.gz data/
```
Copia esse `.tar.gz` pra fora da VPS (Google Drive, etc) periodicamente.

### Parar / reiniciar
```bash
docker compose stop          # para
docker compose start         # liga de novo
docker compose restart       # reinicia
docker compose down          # para + remove containers (dados ficam)
```

---

## 9. Trocar senha do owner (workaround até a UI)

```bash
docker compose exec app python -c "
from web.auth import change_password
change_password('edson.juan.oliversilva@gmail.com', 'NovaSenhaForte123!')
print('OK')
"
```

---

## 10. Firewall (opcional mas recomendado)

```bash
ufw allow 22/tcp     # SSH
ufw allow 80/tcp     # HTTP (Caddy redireciona pra HTTPS)
ufw allow 443/tcp    # HTTPS
ufw allow 443/udp    # HTTP/3
ufw --force enable
```

---

## Troubleshooting

### Caddy não pega certificado
- Confirma que a porta 80 está liberada e o DNS aponta certo: `curl http://insta.seudominio.com.br` deve responder
- Vê o log: `docker compose logs caddy`

### App não sobe
- `docker compose logs app` mostra o erro
- Geralmente é env var faltando ou problema no `.env`

### "Já existe um servidor na porta 80"
- Algum outro serviço (Nginx do sistema, Apache) tá rodando. Para com `systemctl stop nginx` ou similar

### Sessões Instagram somem após restart
- **Não deveriam!** São salvas em `data/sessions/`. Confirma que o volume `./data:/data` no `docker-compose.yml` está correto e que existe a pasta `data/sessions/` no host

---

## Modo B — VPS com Nginx existente

Se sua VPS já tem **Nginx** servindo outros sites, **NÃO use o Caddy** do compose padrão — ele conflitaria nas portas 80/443. Use o arquivo `docker-compose.nginx.yml` (já incluso) que sobe **só o app** ouvindo em `127.0.0.1:8000`, e configura o Nginx existente como reverse proxy.

### B.1. Instalar Docker (se não tiver)
```bash
apt update && apt install -y docker.io docker-compose-plugin git
systemctl enable --now docker
```

### B.2. Clonar e configurar
```bash
mkdir -p /opt && cd /opt
git clone https://github.com/contatoheavenmkt-spec/instapost.git
cd instapost
cp .env.example .env
nano .env
```

Preenche `.env`:
```env
DOMAIN=instapost.shop
EMAIL=seu@email.com
PUBLIC_BASE_URL=https://instapost.shop
```

### B.3. Subir o app (sem Caddy)
```bash
docker compose -f docker-compose.nginx.yml up -d --build
```

Confere:
```bash
docker compose -f docker-compose.nginx.yml ps
curl http://127.0.0.1:8000/api/health   # deve retornar {"ok":true,...}
```

### B.4. Configurar Nginx do host

Copia o template incluso:
```bash
cp /opt/instapost/deploy/nginx-instapost.conf /etc/nginx/sites-available/instapost.shop
```

Edita o `server_name` se for usar outro domínio (default é `instapost.shop www.instapost.shop`).

Habilita o site:
```bash
ln -s /etc/nginx/sites-available/instapost.shop /etc/nginx/sites-enabled/
nginx -t              # testa config (tem que dizer "ok" e "test successful")
systemctl reload nginx
```

### B.5. HTTPS via Let's Encrypt (Certbot)

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d instapost.shop -d www.instapost.shop \
  --non-interactive --agree-tos -m seu@email.com --redirect
```

Certbot edita o config do Nginx sozinho, baixa o cert e configura renovação automática.

Pronto — abre `https://instapost.shop` no navegador.

### Atualizar depois (Modo B)
```bash
cd /opt/instapost
git pull
docker compose -f docker-compose.nginx.yml up -d --build
```

### Backup (Modo B — igual)
```bash
tar czf backup-$(date +%F).tar.gz /opt/instapost/data
```

---

## Sessões "aquecidas" no IP residencial (anti-blacklist)

**Problema:** o Instagram bloqueia logins vindos de IPs de datacenter (Hostinger/AWS/etc). Se você cadastrar conta no painel da VPS e clicar "Conectar", o login sai do IP da VPS — flagged.

**Solução sem proxy:** loga as contas **no seu PC** (IP residencial/4G aceito), copia o arquivo `sessions/CONTA.json` pra VPS, sistema na VPS reaproveita.

### Como funciona

```
1. PC seu (4G)   → Instagram → login com 2FA → sessions/conta.json gerado
2. scp sessions/ → VPS
3. VPS usa a sessão existente → Instagram aceita os posts
```

O Instagram pode mostrar uma notificação "Você logou de um lugar novo?" na primeira postagem — confirma "Sim" no app/email **uma vez** e nunca mais pede.

### Passo a passo

**1. No seu PC, conecta as contas localmente:**
```powershell
cd C:\Users\tutif\Desktop\insta-poster
.\venv\Scripts\python.exe run.py
# Abre http://127.0.0.1:8000 → Contas → adiciona + Conectar
# Repete pra cada conta
```

Confirma que os arquivos foram gerados:
```powershell
dir sessions\
```

**2. Sobe as sessões pra VPS (1 comando):**
```powershell
.\deploy\sync-sessions.ps1
```

Vai listar as sessões + pedir confirmação + senha SSH da VPS.

**3. No painel da VPS** (`https://instapost.shop`):
- Adiciona as MESMAS contas (mesmos email/senha/chave 2FA)
- **NÃO clica em "Conectar"** — sistema já vai usar a sessão copiada
- Pode disparar posts direto

### Equipe inteira no mesmo esquema

Cada membro da equipe pode:
1. Clonar o repo no PC
2. Conectar as contas DELE localmente
3. Rodar `sync-sessions.ps1` apontando pra VPS
4. As contas vão entrar no painel central com sessão real

Assim a "frota" de IPs residenciais alimenta o sistema central — sem proxy pago.

### Quando precisa renovar a sessão

Sessão Instagram dura semanas/meses, mas eventualmente expira (ou ele "desloga" remoto). Quando isso acontecer, você verá no log do disparo: `Sessão expirada, fazendo login novo` → na VPS isso falha (IP blacklisted).

Faz o mesmo processo de novo: conecta no PC → roda `sync-sessions.ps1`.
