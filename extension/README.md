# Insta Poster — Extensão Chrome

Captura cookies do `instagram.com` e envia direto pro painel.
Funciona em **qualquer Chrome** — sem precisar do worker local.

## Instalar

1. Baixa o ZIP no painel: **Contas → Extensão → baixa `insta-poster-extension.zip`**
2. Descompacta numa pasta
3. No Chrome: `chrome://extensions`
4. Liga **Modo do desenvolvedor** (canto superior direito)
5. Clica **Carregar sem compactação** → escolhe a pasta descompactada
6. Fixa o ícone na barra (📌)

## Usar

1. Gera o **token** uma vez no painel (Contas → Extensão → Gerar token)
2. Clica no ícone da extensão → cola URL do painel + token
3. Loga na conta Insta normalmente em qualquer aba
4. Volta na extensão, escolhe **workspace** + **conta** + clica **Salvar cookies**
5. Pronto — o worker (local ou VPS) já pode postar nessa conta

## Como funciona

- Extensão lê **todos os cookies** de `.instagram.com` via `chrome.cookies.getAll`
  (inclui `sessionid` HttpOnly que JS normal não vê)
- Envia pro painel via `POST /api/sessions/upload` com `Authorization: Bearer <token>`
- Painel monta `session.json` no formato instagrapi com `manually_saved: true`
- Worker carrega como sessão manual e usa direto (sem login API)

## Privacidade

- Cookies SÓ vão pro seu painel (URL que vc colou na config)
- Token fica em `chrome.storage.local` — não sincroniza entre dispositivos
- Nenhum analytics/telemetria

## Dev

Pra rebuildar ícones do zero: `python make_icons.py`
