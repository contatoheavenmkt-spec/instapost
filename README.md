# Insta Poster

Sistema SaaS-style (localhost) pra postar Reels em massa em múltiplas contas Instagram.
Tem CLI (`post.py`, `test_login.py`) **e** Web UI (`run.py`).

---

## Quick start (Web UI)

```bash
cd insta-poster
venv\Scripts\activate           # Windows
# source venv/bin/activate      # Mac/Linux

python run.py
```

Abre: **http://127.0.0.1:8000** (local) ou **http://<seu-ip-lan>:8000** (acessível do celular na mesma rede).

A UI tem:
- **Dashboard** — visão geral (contas / pendentes / postados / jobs)
- **Contas** — CRUD de contas, testar login, limpar sessão, pausar/ativar
- **Vídeos** — upload de .mp4 + legenda, fila pendente e arquivo de postados
- **Disparar / Jobs** — dispara o `post.py` com filtros (conta, vídeo, dry-run), histórico e log ao vivo
- **Logs** — leitor dos arquivos `logs/*.log`

---

## Setup do zero

### 1. Python 3.10+
```bash
winget install Python.Python.3.12    # ou baixe em python.org
```

### 2. Venv + deps
```bash
cd insta-poster
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 3. (opcional) Rodar via CLI direto
```bash
python test_login.py conta1     # testa 1 conta, salva sessão
python post.py --dry-run        # simula sem postar
python post.py                  # posta tudo de verdade
```

---

## Como funciona

- `core/session.py` faz login com **persistência de sessão** em `sessions/<user>.json`. Regra de ouro: nunca relogar do zero se já tem sessão (login repetido = checkpoint quase certo).
- `core/poster.py` é o wrapper de `cl.clip_upload` (Reels).
- `post.py` itera vídeos × contas com **jitter aleatório** (60-180s entre contas, 5-15min entre vídeos).
- `web/main.py` (FastAPI) expõe tudo via HTTP. Disparos rodam como **subprocess** isolado — o servidor não trava.
- `web/jobs.py` gerencia jobs em memória + snapshot em `logs/jobs.json` (sobrevive a restart).

## Estrutura

```
insta-poster/
├─ run.py                    ← entry point do web server (uvicorn)
├─ post.py                   ← CLI principal
├─ test_login.py             ← teste de login isolado
├─ accounts.json             ← contas (criado pela UI, gitignored)
├─ core/
│  ├─ session.py             ← login + persistência de sessão
│  └─ poster.py              ← wrapper instagrapi
├─ web/
│  ├─ main.py                ← FastAPI app + rotas
│  ├─ jobs.py                ← gerenciador de subprocess jobs
│  └─ templates/             ← Jinja (dashboard, accounts, videos, jobs, logs)
├─ content/
│  ├─ pending/               ← fila .mp4 + .txt (legenda)
│  └─ posted/                ← arquivo dos que já foram
├─ sessions/                 ← sessões salvas (NÃO commitar)
├─ logs/                     ← logs do post.py + snapshot de jobs
└─ venv/                     ← ambiente Python
```

---

## ⚠️ Avisos importantes

- **Sem proxy (modo atual):** OK pras primeiras contas, mas **vai começar a tomar checkpoint** conforme escalar. Tenha proxy residencial pronto pra próxima fase.
- **Conta nova:** alta chance de pedir verificação no primeiro login. **Faça login manual** no app/web antes de testar aqui pra "aquecer".
- **2FA:** SMS não funciona (instagrapi não consegue). Use 2FA por app (TOTP) ou desligue.
- **Variação de vídeo:** o MVP **não varia** o .mp4 entre contas. Postar arquivo idêntico em N contas é flag forte. Variação por FFmpeg fica pra próxima versão.
- **Challenge email:** se a UI mostrar "challenge", o instagrapi ficou bloqueado esperando código. Rode `python test_login.py <user>` no terminal pra digitar o código manualmente.
