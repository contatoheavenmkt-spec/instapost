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

    # Device fingerprint determinístico por conta: cada @ aparece como
    # um celular Android diferente (modelo, versão, IDs). Reduz cluster
    # detection do Instagram.
    try:
        from core.devices import apply_device_to_client
        device_info = apply_device_to_client(cl, username)
        if device_info:
            print(f"[{username}] 📱 device: {device_info['manufacturer']} {device_info['model']} (Android {device_info['android_release']})")
    except Exception as e:
        print(f"[{username}] ⚠️ device fingerprint falhou: {e} — usando default do instagrapi")

    # Normaliza defensivamente — server pode mandar formato esquisito
    # (host:port:user:pass do DataImpulse, com ou sem http:// na frente)
    proxy = _normalize_proxy(proxy)
    if proxy:
        cl.set_proxy(proxy)
        print(f"[{username}] 🌐 proxy ativo: {proxy[:40]}...")
    if challenge_handler:
        cl.challenge_code_handler = challenge_handler

    totp_secret = _clean_totp_secret(totp_secret)
    session_file = SESSIONS_DIR / f"{username}.json"

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
        # Re-aplica device fingerprint no Client novo (fresh login)
        try:
            from core.devices import apply_device_to_client
            apply_device_to_client(cl, username)
        except Exception:
            pass
        if proxy:
            cl.set_proxy(proxy)

        _do_login(cl)
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
