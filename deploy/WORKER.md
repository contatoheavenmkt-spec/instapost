# Worker — instalação no PC de cada membro

O **worker** é um pequeno programa Python que roda no PC de cada pessoa da equipe. Ele se conecta ao painel central (`https://instapost.shop`), pega disparos da fila e executa **usando o IP residencial do PC** — assim o Instagram não bloqueia (que é o que acontece quando o servidor da VPS tenta logar).

## Pré-requisitos

- **Python 3.10+** instalado no PC (https://python.org)
- **Token de worker** gerado pelo admin no painel: **Admin → Workers → Novo worker**
- Conexão internet (de preferência fixa/wifi residencial ou 4G; **não** usar VPN/proxy datacenter)

## Instalação (uma vez)

### 1. Clona o projeto

```bash
git clone https://github.com/contatoheavenmkt-spec/instapost.git
cd instapost
```

### 2. Cria venv + instala deps mínimas

```bash
# Windows
python -m venv venv
venv\Scripts\activate
pip install -r worker-requirements.txt

# Linux/Mac
python3 -m venv venv
source venv/bin/activate
pip install -r worker-requirements.txt
```

### 3. Cria o `.env` com seu token

Dentro da pasta `instapost/`, cria um arquivo `.env` (sem .txt no fim, é só ".env"):

```env
SERVER_URL=https://instapost.shop
WORKER_TOKEN=cole_seu_token_aqui
WORKER_NAME=PC do João
```

- O **token** você pega no painel: **Admin → Workers → Novo worker → Copiar token** (só aparece uma vez, anota direito).
- **SERVER_URL** é o domínio do painel (sem barra no final).
- **WORKER_NAME** é só pra você se identificar no painel (default: nome do PC).

## Como rodar

Sempre que quiser receber jobs, abre o terminal na pasta:

```bash
cd caminho/pra/instapost
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Mac/Linux
python worker.py
```

Vai aparecer:

```
============================================================
  Insta Poster Worker
============================================================
  Server:   https://instapost.shop
  Nome:     PC do João
  Platform: Windows 11
============================================================

✓ Conectado (worker_id: wk_abc123)
  Aguardando jobs... (poll 5s)
```

Deixa o terminal aberto. Quando alguém criar um disparo via "Postar via worker" no painel, **seu PC pega o job** e executa.

Pra parar: `Ctrl+C`.

## Fluxo prático

1. **Admin no painel** vai em Biblioteca, abre um vídeo, clica **"Postar via worker"**, escolhe a conta
2. Job entra na **fila central**
3. **Seu worker** (rodando aqui no PC) faz polling a cada 5s, pega o job
4. Worker **baixa a mídia** do servidor, faz login no Instagram com seu IP residencial, posta
5. Resultado vai pro painel (aparece em **Disparos**)

## Múltiplos workers da equipe

Cada membro da equipe pode rodar **um worker próprio** no PC dele:

1. Admin gera **um token diferente** pra cada pessoa (no painel)
2. Cada um instala seguindo este guia, com **seu próprio token** no `.env`
3. Quando um job entra na fila, o **primeiro worker online** que faz polling pega
4. Todos vocês veem o status no painel

## Auto-start no Windows (opcional)

Pra o worker iniciar automaticamente com o Windows:

1. Cria um arquivo `start-worker.bat` na pasta do projeto:
```batch
@echo off
cd /d C:\caminho\pra\instapost
call venv\Scripts\activate
python worker.py
pause
```

2. Pressiona `Win+R`, digita `shell:startup`, Enter
3. Cria um atalho do `.bat` na pasta que abriu

Pronto — vai rodar toda vez que ligar o PC.

## Solução de problemas

### "Token inválido"
- Token foi revogado pelo admin → pede outro
- Copiou errado → confere o `.env`

### "Falha login Instagram"
- Conta com 2FA mas chave não cadastrada → admin cadastra a chave 2FA no painel
- IP do seu PC também tá flagged → tenta de outro Wi-Fi ou 4G
- Conta bloqueada → resolver no app primeiro

### "Heartbeat falhou"
- Sem internet
- Domínio fora do ar → verifica `https://instapost.shop` no browser
- Firewall bloqueando → libera Python no firewall do Windows

### Worker pega job mas não posta
- Verifica os logs no terminal — mensagem de erro deve aparecer
- Job vai pra status "error" no painel com a mensagem
