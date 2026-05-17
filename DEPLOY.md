# Deploy em VPS (Hostinger Brasil + Docker + Caddy)

Guia prático pra subir o Insta Poster numa VPS com HTTPS automático.

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
