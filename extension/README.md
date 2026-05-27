# Insta Poster — Extensão Chrome (v1.1)

Captura cookies do `instagram.com` **e configura o proxy da conta automaticamente** antes do login.
Funciona em **qualquer Chrome** — sem precisar do worker local.

## O que mudou na v1.1

Agora a extensão configura o proxy da conta no Chrome ANTES de você logar no Instagram.
Resultado: o IP que faz login = IP que a VPS usa pra postar. **Sem mismatch.**

## Instalar

1. Baixa o ZIP no painel: **Contas → Extensão → baixa `insta-poster-extension.zip`**
2. Descompacta numa pasta
3. No Chrome: `chrome://extensions`
4. Liga **Modo do desenvolvedor** (canto superior direito)
5. Clica **Carregar sem compactação** → escolhe a pasta descompactada
6. Fixa o ícone na barra (📌)

## Permissões da extensão

- `cookies` — ler cookies do instagram.com (incluindo HttpOnly)
- `storage` — guardar URL do painel + token localmente
- `proxy` — configurar proxy do Chrome temporariamente
- `webRequest` — responder challenge HTTP do proxy (auth)
- `tabs` — abrir nova aba pro Instagram

## Usar (fluxo de 4 passos)

### Setup inicial (uma vez)

1. Painel: **Contas → Extensão → Gerar token** → copia
2. Clica no ícone da extensão → cola URL do painel + token

### Pra cada conta (uma vez por conta, depois renova a cada 60-90d)

1. Cadastra a conta no painel (com proxy configurado)
2. Abre a extensão → seleciona **workspace** + **conta**
3. Clica **"Aplicar proxy + abrir Insta"** — Chrome inteiro passa a usar o proxy da conta, uma aba abre em `instagram.com/login`
4. Loga normalmente (todo o tráfego vai pelo proxy)
5. Volta na extensão → clica **"Salvar cookies"**
6. Cookies vão pra VPS, proxy é limpo automaticamente

## Como funciona

```
Você seleciona @X na extensão
         ↓
Extensão pega proxy da @X via /api/sessions/extension-info
         ↓
Extensão aplica chrome.proxy.settings com esse proxy
         ↓
Aba nova abre em instagram.com (passando pelo proxy)
         ↓
Você loga — IG vê o IP do proxy (não seu IP real)
         ↓
Você clica "Salvar cookies"
         ↓
Cookies vão pro painel via /api/sessions/upload
         ↓
Extensão limpa o proxy (Chrome volta ao normal)
         ↓
VPS posta usando cookies + mesmo proxy = sem mismatch ✓
```

## Avisos

- **Enquanto o proxy tá ativo**, TODO o tráfego do Chrome passa por ele. Isso inclui outras abas (YouTube, Gmail, etc). Funciona, mas mais devagar e roteado pelo proxy. Por isso a extensão limpa automaticamente depois de salvar cookies.
- Se vc fechar a extensão sem salvar cookies, o proxy continua ativo. Use **"Limpar proxy"** pra resetar.
- Cookies HttpOnly (incluindo `sessionid`) são lidos via `chrome.cookies.getAll` (única forma confiável — JavaScript de página não acessa HttpOnly).

## Privacidade

- Cookies SÓ vão pro seu painel (URL que vc colou na config)
- Token fica em `chrome.storage.local` — não sincroniza entre dispositivos
- Proxy é setado no escopo `regular` do Chrome (não afeta incognito, não afeta outros browsers)
- Nenhum analytics/telemetria

## Dev

Pra rebuildar ícones: `python make_icons.py`
