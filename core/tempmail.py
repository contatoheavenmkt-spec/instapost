"""
Busca código de verificação do Instagram em emails do tempmail.plus.

Suporta domínios: mailto.plus, tempmail.plus, e outros do mesmo provedor.

Uso:
    from core.tempmail import fetch_instagram_code
    code = fetch_instagram_code("usuario@mailto.plus", timeout=120)
    # code = "123456" ou None se não encontrou
"""
import re
import time
from typing import Optional

import requests

TEMPMAIL_API = "https://tempmail.plus/api/mails"

# Regex pra capturar código de 6 ou 8 dígitos do Instagram
# Instagram envia "Use XXX XXX to verify..." ou "XXX-XXX" ou "XXXXXXXX"
CODE_PATTERNS = [
    re.compile(r"(\d{3})\s+(\d{3})"),       # "123 456"
    re.compile(r"(\d{3})-(\d{3})"),          # "123-456"
    re.compile(r"\b(\d{6})\b"),              # "123456"
    re.compile(r"\b(\d{8})\b"),              # "12345678" (código mais longo)
]

# Subjects que indicam email de verificação do Instagram
INSTAGRAM_SUBJECTS = [
    "instagram",
    "verification",
    "security code",
    "login code",
    "confirm",
    "código",
    "codigo",
    "verificação",
    "verificacao",
]


def _extract_code(text: str) -> Optional[str]:
    """Extrai código de verificação do texto do email."""
    if not text:
        return None
    for pat in CODE_PATTERNS:
        m = pat.search(text)
        if m:
            groups = m.groups()
            if len(groups) == 2:
                return groups[0] + groups[1]  # "123" + "456" = "123456"
            return groups[0]
    return None


def _is_instagram_email(mail: dict) -> bool:
    """Verifica se o email é do Instagram (subject ou remetente)."""
    from_mail = (mail.get("from_mail") or "").lower()
    subject = (mail.get("subject") or "").lower()
    # Remetente do Instagram
    if "instagram" in from_mail or "facebookmail" in from_mail:
        return True
    # Subject com palavras-chave
    for kw in INSTAGRAM_SUBJECTS:
        if kw in subject:
            return True
    return False


def fetch_instagram_code(
    email: str,
    timeout: int = 120,
    poll_interval: int = 5,
    since_timestamp: float = None,
) -> Optional[str]:
    """
    Busca código de verificação do Instagram na caixa do tempmail.plus.

    Args:
        email: endereço email (ex: "usuario@mailto.plus")
        timeout: segundos máximos pra esperar o email chegar
        poll_interval: intervalo entre polls (default 5s)
        since_timestamp: só considera emails após esse timestamp (default: agora - 5min)

    Returns:
        Código de verificação (ex: "123456") ou None se não encontrou no timeout.
    """
    if not email:
        return None

    if since_timestamp is None:
        since_timestamp = time.time() - 300  # últimos 5 minutos

    deadline = time.time() + timeout
    seen_ids = set()

    print(f"[tempmail] buscando código pra {email} (timeout {timeout}s)")

    while time.time() < deadline:
        try:
            r = requests.get(
                TEMPMAIL_API,
                params={"email": email, "limit": 10},
                timeout=15,
            )
            if r.status_code != 200:
                print(f"[tempmail] API retornou {r.status_code}")
                time.sleep(poll_interval)
                continue

            data = r.json()
            mail_list = data.get("mail_list") or []

            for mail in mail_list:
                mail_id = mail.get("mail_id")
                if mail_id in seen_ids:
                    continue
                seen_ids.add(mail_id)

                # Verifica se é do Instagram
                if not _is_instagram_email(mail):
                    continue

                # Busca o conteúdo completo do email
                try:
                    detail_r = requests.get(
                        f"{TEMPMAIL_API}/{mail_id}",
                        params={"email": email},
                        timeout=15,
                    )
                    if detail_r.status_code != 200:
                        continue
                    detail = detail_r.json()
                except Exception:
                    continue

                # Tenta extrair código do texto ou HTML
                code = _extract_code(detail.get("text") or "")
                if not code:
                    code = _extract_code(detail.get("html") or "")
                if code:
                    print(f"[tempmail] código encontrado: {code} (de {mail.get('from_mail')})")
                    return code

        except Exception as e:
            print(f"[tempmail] erro: {e}")

        remaining = int(deadline - time.time())
        if remaining > 0:
            print(f"[tempmail] aguardando email... ({remaining}s restantes)")
        time.sleep(poll_interval)

    print(f"[tempmail] timeout — código não encontrado em {timeout}s")
    return None
