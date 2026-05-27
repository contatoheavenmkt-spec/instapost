# Insta Poster — Extensão Chrome (v1.3)

Sincroniza cookies de qualquer Chrome com o seu painel. Sistema **estilo Dolphin Anty**:
clica numa conta, extensão configura o proxy + baixa cookies da VPS + injeta no
Chrome local. Resultado: **Instagram abre já logado naquela conta**, em qualquer PC.

## O que tem na v1.3

- ✅ Conta com cookies na VPS → **abre direto logada** (zero login manual)
- ✅ Conta sem cookies → fluxo antigo (proxy + login manual + Salvar cookies)
- ✅ Workspace-scoped: cada workspace tem suas próprias contas
- ✅ Proxy automático antes do login (cookies nascem no IP certo)
- ✅ Indicador visual: 🔓 = tem cookies / 🔒 = ainda não tem

## Instalar

1. Painel: **Contas → Extensão → baixa o ZIP**
2. Descompacta numa pasta
3. Chrome: `chrome://extensions` → liga modo desenvolvedor → "Carregar sem compactação"
4. Aceita as permissões (cookies, proxy, webRequest)
5. Fixa o ícone na barra

## Setup (uma vez por browser)

1. Painel: **Contas → Extensão → Gerar token**
2. Cola URL do painel + token na extensão

## Usar

### Caso 1: conta JÁ tem cookies na VPS (badge 🔓)

1. Clica na conta na lista da extensão
2. Extensão: aplica proxy → baixa cookies → injeta → abre IG
3. **Você vê a conta já logada** ✨

Se ao abrir o IG redirecionar pra `/login`, significa que os cookies expiraram.
Daí loga manual + clica "Salvar cookies" pra renovar.

### Caso 2: conta SEM cookies ainda (badge 🔒)

1. Clica na conta
2. Extensão: aplica proxy → abre `instagram.com/login`
3. Você loga manualmente (email + senha + 2FA)
4. Volta na extensão → clica **"Salvar cookies dessa conta"**
5. Cookies vão pra VPS
6. **Próximos acessos** (nesse PC ou em outro) caem no Caso 1

## Fluxo end-to-end

```
PC A (primeira vez):
  1. Cadastra @suzana no painel (com proxy)
  2. Extensão → clica @suzana (badge 🔒)
  3. Proxy aplicado + abre IG → loga manual
  4. Volta extensão → "Salvar cookies"
  5. Cookies guardados na VPS ✓

PC B (outro dia, outro lugar):
  1. Instala extensão + cola token
  2. Extensão → vê @suzana (badge 🔓)
  3. Clica @suzana → proxy + download cookies + injeta + abre IG
  4. Já tá logada — sem digitar nada ✨

VPS (independente):
  - Posta @suzana 24/7 usando os cookies da VPS
  - Não precisa de PC ligado
```

## Workspace isolation

A extensão lista só as contas do **workspace selecionado**. Se vc tem 2+ workspaces, dropdown aparece pra escolher. Se só tem 1, dropdown some (caso comum).

## O que a extensão guarda

| Storage | O que |
|---|---|
| `chrome.storage.local` | URL do painel + token + último workspace usado |
| (nada mais) | Cookies do IG ficam só no Chrome cookie store padrão |

Cookies NÃO são guardados pela extensão — são lidos do Chrome e enviados pra VPS, e baixados da VPS pra injetar quando vc quer abrir.

## Permissões necessárias

- `cookies`: ler cookies do instagram.com (incluindo HttpOnly como sessionid)
- `proxy`: configurar proxy do Chrome temporariamente pra cada conta
- `webRequest`: responder challenge HTTP 407 do proxy (auth basic)
- `tabs`: abrir nova aba pro IG
- `storage`: salvar URL+token localmente
- `<all_urls>`: necessário pelo `webRequest` quando proxy tá ativo

## Avisos importantes

- **Enquanto proxy tá ativo, TODO Chrome vai pelo proxy** — outras abas ficam mais devagar. Extensão limpa automaticamente após salvar cookies (1.5s).
- **Cookies HttpOnly** (sessionid principalmente) NÃO são acessíveis via JavaScript de página. A extensão usa `chrome.cookies` API, que é a única forma confiável de ler/escrever HttpOnly cookies em Chrome.
- **Trocar de conta**: clicar em outra conta enquanto proxy ainda tá ativo → extensão pergunta se quer trocar. Limpa cookies+proxy atual antes de aplicar o novo (evita misturar sessões).

## Privacidade

- Cookies SÓ vão pro seu painel (URL que vc colou na config)
- Token fica em `chrome.storage.local` — não sincroniza entre dispositivos
- Proxy é setado no escopo `regular` (não afeta incognito nem outros browsers)
- Nenhum analytics / telemetria

## Dev

Pra rebuildar ícones: `python make_icons.py`
