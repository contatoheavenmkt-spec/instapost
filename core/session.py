"""
Gerencia login e persistência de sessão.
A regra de ouro: NUNCA logar do zero se já tem sessão salva.
Login repetido = checkpoint quase certo.

Suporte a 2FA TOTP: se a conta tiver `totp_secret` (a chave que o vendedor
de contas fornece, ex: "XMO3 LBDQ ECDF CZL2 SU5M NEZ4 SIXE 4QXU"), o código
de 6 dígitos é gerado e enviado automaticamente no login.
"""
import json
import os
import re
from pathlib import Path
from typing import Optional

from instagrapi import Client
from instagrapi.exceptions import LoginRequired, ChallengeRequired

from core.paths import SESSIONS_DIR, ACCOUNTS_FILE


def _clean_totp_secret(secret: Optional[str]) -> Optional[str]:
    """Remove espaços, hífens e normaliza pra maiúscula. Os vendedores costumam
    entregar a chave formatada em blocos de 4 caracteres com espaços."""
    if not secret:
        return None
    cleaned = re.sub(r"[\s-]+", "", secret).upper()
    return cleaned or None


def _normalize_proxy(raw: Optional[str]) -> Optional[str]:
    """Converte formatos comuns de proxy pra URL padrão.

    Defensivo: o worker recebe proxy do server e nem sempre o server normalizou
    (ex: VPS rodando código antigo). Aqui garantimos que o instagrapi/requests
    sempre vê o formato URL correto antes de tentar usar.

    Aceita:
      - http://user:pass@host:port           (já no formato URL)
      - socks5://user:pass@host:port         (idem)
      - http://host:port:user:pass           (DataImpulse + http:// na frente)
      - host:port:user:pass                  (DataImpulse, Bright Data raw)
      - user:pass@host:port                  (sem scheme — vira http://)
      - host:port                            (sem auth — vira http://)
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        scheme = scheme.lower()
        if scheme not in ("http", "https", "socks4", "socks5", "socks5h"):
            scheme = "http"
    else:
        scheme = "http"
        rest = raw
    if "@" in rest:
        return f"{scheme}://{rest}"
    parts = rest.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"{scheme}://{user}:{password}@{host}:{port}"
    if len(parts) == 2:
        return f"{scheme}://{rest}"
    return raw


def get_client(
    username: str,
    password: str,
    proxy: str = None,
    totp_secret: Optional[str] = None,
    challenge_handler=None,
    totp_fallback_handler=None,
) -> Client:
    """
    Retorna cliente logado. Tenta usar sessão salva primeiro.
    Se a sessão expirou, faz login novo (com TOTP se disponível) e salva.

    Args:
        challenge_handler: callback(username, choice) -> code para responder
            challenges de email/SMS do Instagram. Se None, o instagrapi usa input().
        totp_fallback_handler: callback() -> code pedido se a conta tem 2FA mas
            totp_secret não foi cadastrado. Se None, levanta exceção.
    """
    cl = Client()
    cl.delay_range = [2, 5]

    session_file = SESSIONS_DIR / f"{username}.json"
    session_existed = session_file.exists()

    # Device fingerprint: APENAS pra contas SEM sessão prévia.
    # Pra contas COM sessão, mantém o device que a conta JÁ TEM (load_settings
    # carrega no Tentativa 1; pra Tentativa 2 fresh-login, lemos manualmente).
    # Por que: mudar device numa conta que IG já conhece = "novo aparelho
    # detectado" = login pra verificação. Isso afundou contas que antes
    # logavam normal — random device aplicado em conta com histórico antigo.
    if not session_existed:
        try:
            from core.devices import apply_device_to_client
            device_info = apply_device_to_client(cl, username)
            if device_info:
                print(f"[{username}] 📱 device random (1ª vez): {device_info['manufacturer']} {device_info['model']}")
        except Exception as e:
            print(f"[{username}] ⚠️ device fingerprint falhou: {e} — usando default do instagrapi")

    # Normaliza defensivamente — server pode mandar formato esquisito
    # (host:port:user:pass do DataImpulse, com ou sem http:// na frente)
    proxy = _normalize_proxy(proxy)

    # Lê "sticky attempt" salvo (qual IP do pool já provou que funciona pra
    # essa conta). Se sessão anterior teve que rotacionar até achar IP limpo,
    # esse arquivinho lembra qual foi.
    proxy_base = proxy  # mantém base pra rotação
    sticky_attempt_file = SESSIONS_DIR / f"{username}_sticky.txt"
    sticky_attempt = 0
    if sticky_attempt_file.exists():
        try:
            sticky_attempt = int(sticky_attempt_file.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            sticky_attempt = 0

    if proxy:
        # CRÍTICO: força sticky session por conta. Provedor rotativo (default
        # DataImpulse, BrightData, etc.) dá IP DIFERENTE cada request -> Insta
        # vê sessão pulando IP -> checkpoint imediato. Com sticky, cada @ sai
        # de UM IP fixo, contas diferentes saem de IPs diferentes.
        try:
            from core.proxy_sticky import make_sticky, detect_provider
            sticky = make_sticky(proxy_base, username, attempt=sticky_attempt)
            if sticky != proxy:
                provider = detect_provider(proxy) or "?"
                tag = f"#{sticky_attempt+1}" if sticky_attempt > 0 else ""
                print(f"[{username}] 🔒 sticky session aplicado ({provider}) {tag}")
                proxy = sticky
        except Exception as e:
            print(f"[{username}] ⚠️ sticky session falhou: {e} (usando proxy rotativo)")
        cl.set_proxy(proxy)
        print(f"[{username}] 🌐 proxy ativo: {proxy[:60]}...")
    if challenge_handler:
        cl.challenge_code_handler = challenge_handler

    totp_secret = _clean_totp_secret(totp_secret)
    # session_file já definido lá em cima (precisava antes do device check)

    def _do_login(client: Client):
        """Faz o login passando código TOTP se a conta tem 2FA. Wrapped em
        retry_on_429 — se Insta retornar rate limit, espera backoff exponencial."""
        from core.retry import with_retry

        def _attempt_login():
            if totp_secret:
                try:
                    code = client.totp_generate_code(totp_secret)
                except Exception as e:
                    raise RuntimeError(f"Falha gerando código TOTP (chave inválida?): {e}")
                import time as _time
                seconds_left = 30 - int(_time.time()) % 30
                print(f"[{username}] 🔐 Código TOTP gerado: {code} (expira em {seconds_left}s)")
                print(f"[{username}]    Senha sendo enviada: [{len(password)} chars] (oculta por segurança)")
                print(f"[{username}]    Abra o 2fa.ac com sua chave AGORA e confirme que mostra '{code}'.")
                client.login(username, password, verification_code=code)
            elif totp_fallback_handler:
                try:
                    client.login(username, password)
                except Exception as e:
                    if "two_factor" in str(e).lower() or "2fa" in str(e).lower():
                        manual_code = totp_fallback_handler()
                        client.login(username, password, verification_code=manual_code)
                    else:
                        raise
            else:
                client.login(username, password)

        # Retry: 3 tentativas, base 4s, max 5min — só pra erros de rate limit.
        # Challenge/banned/etc levantam direto sem retry.
        with_retry(_attempt_login, max_retries=3, base_delay=4.0, max_delay=300.0, label=f"login:{username}")

    # Tentativa 1: usar sessão salva (SEM refazer login)
    # File lock garante atomicidade: 2 jobs paralelos pra mesma conta esperam.
    from core.file_lock import file_lock as _file_lock
    if session_file.exists():
        try:
            with _file_lock(session_file, timeout=15):
                cl.load_settings(session_file)
                # Set creds pra que, em LoginRequired, o instagrapi possa relogar sozinho
                cl.username = username
                cl.password = password
            # Teste leve da sessão. Se válida, segue sem TOTP. (FORA do lock pra
            # não segurar 5s+ enquanto a HTTP request roda)
            cl.get_timeline_feed()
            print(f"[{username}] Sessão restaurada ✓ (sem relogin)")
            return cl
        except LoginRequired:
            print(f"[{username}] Sessão expirou, fazendo login novo...")
        except Exception as e:
            print(f"[{username}] Sessão inválida ({e}), refazendo...")

    # Tentativa 2: login do zero
    try:
        cl = Client()
        cl.delay_range = [2, 5]
        # Device: se já tinha sessão (mesmo expirada), reusa o device DELA
        # pra IG ver continuidade. Se conta nova (sem sessão), aplica random.
        if session_existed:
            try:
                import json as _json
                old = _json.loads(session_file.read_text(encoding="utf-8"))
                if old.get("device_settings"):
                    cl.set_device(old["device_settings"])
                if old.get("user_agent"):
                    cl.set_user_agent(old["user_agent"])
                if old.get("locale"):
                    cl.set_locale(old["locale"])
                if old.get("timezone_offset") is not None:
                    cl.set_timezone_offset(old["timezone_offset"])
                if old.get("uuids"):
                    cl.set_uuids(old["uuids"])
                ds = old.get("device_settings") or {}
                print(f"[{username}] 📱 device da sessão antiga reusado: {ds.get('manufacturer','?')} {ds.get('model','?')} (continuidade IG)")
            except Exception as e:
                print(f"[{username}] ⚠️ não consegui ler device da sessão antiga ({e}), usando random")
                try:
                    from core.devices import apply_device_to_client
                    apply_device_to_client(cl, username)
                except Exception:
                    pass
        else:
            # Conta nunca logou — random device + UUIDs novos
            try:
                from core.devices import apply_device_to_client
                apply_device_to_client(cl, username)
            except Exception:
                pass

        # Login com IP ROTATION: se DataImpulse IP atual estiver na blacklist
        # do IG, tenta proximo IP do pool (sid attempt+1) até 5x.
        MAX_STICKY_ATTEMPTS = 5
        winning_attempt = None
        for ip_try in range(sticky_attempt, sticky_attempt + MAX_STICKY_ATTEMPTS):
            # Re-aplica proxy com sticky attempt ip_try
            if proxy_base:
                try:
                    from core.proxy_sticky import make_sticky
                    proxy_sticky = make_sticky(proxy_base, username, attempt=ip_try)
                    cl.set_proxy(proxy_sticky)
                    if ip_try > sticky_attempt:
                        print(f"[{username}] 🔄 tentando IP #{ip_try + 1} do pool DataImpulse")
                except Exception:
                    cl.set_proxy(proxy_base)
            try:
                _do_login(cl)
                winning_attempt = ip_try
                break  # sucesso
            except Exception as e:
                msg = str(e).lower()
                # IP blacklist OU challenge "change your IP" → tenta outro IP do pool
                if ("blacklist" in msg or "change your ip" in msg) and (ip_try - sticky_attempt) < MAX_STICKY_ATTEMPTS - 1:
                    print(f"[{username}] 🚫 IP #{ip_try + 1} blacklist/queimado — tentando próximo do pool em 2s")
                    import time as _t2
                    _t2.sleep(2)
                    continue
                raise

        # Salva o "winning attempt" pra reusar nas próximas sessões dessa conta
        if winning_attempt is not None and winning_attempt != sticky_attempt:
            try:
                sticky_attempt_file.write_text(str(winning_attempt), encoding="utf-8")
                print(f"[{username}] 💾 salvando winning sticky #{winning_attempt + 1} pra próximas sessões")
            except Exception:
                pass

        # Dump sessão sob file lock — evita corrupção se 2 procs salvarem juntos
        with _file_lock(session_file, timeout=15):
            cl.dump_settings(session_file)
        kind = "com 2FA" if totp_secret else "sem 2FA"
        print(f"[{username}] Login novo ✓ ({kind}, sessão salva)")
        return cl

    except ChallengeRequired:
        print(f"[{username}] ⚠️  CHALLENGE: Instagram pediu verificação por email/SMS.")
        print(f"   Isso acontece quando a conta é nova, ou foi flagada, ou logou de IP novo.")
        print(f"   Faça login manual no app/web pra resolver e tente de novo.")
        raise
    except Exception as e:
        msg = str(e).lower()
        if "blacklist" in msg or "change your ip" in msg:
            print(f"[{username}] 🚫 IP BLACKLISTED — Instagram bloqueou seu IP residencial.")
            print(f"   Isso é diferente de senha errada. A mensagem 'password is incorrect' aqui é mentira do Instagram.")
            print(f"   Causa comum: várias tentativas de login falhadas seguidas (incluindo as anteriores ao 2FA).")
            print(f"   Soluções:")
            print(f"     1. Esperar 1-24h e tentar de novo (mais comum)")
            print(f"     2. Usar proxy residencial nessa conta (campo 'Proxy' na UI)")
            print(f"     3. Reiniciar o roteador (pra pegar IP novo da operadora — funciona se for IP dinâmico)")
            print(f"     4. Usar 4G/hotspot do celular como teste")
        elif "two_factor" in msg or "2fa" in msg or "verification_code" in msg:
            print(f"[{username}] ⚠️  Essa conta tem 2FA ativado. Cadastre a chave 2FA (TOTP) na UI.")
        elif "checkpoint" in msg or "challenge" in msg:
            print(f"[{username}] ⚠️  Instagram pediu verificação adicional (checkpoint).")
            print(f"   Faça login manual no app/web pra resolver.")
        print(f"[{username}] ❌ Falha no login: {e}")
        raise


def load_accounts(path: str = None) -> list:
    """Carrega lista de contas do JSON (default: ACCOUNTS_FILE de core/paths)."""
    target = Path(path) if path else ACCOUNTS_FILE
    if not target.exists():
        raise FileNotFoundError(
            f"Arquivo {target} não encontrado. "
            f"Adicione contas pela UI ou copie accounts.example.json."
        )
    with open(target) as f:
        accounts = json.load(f)
    return [a for a in accounts if a.get("active", True)]
