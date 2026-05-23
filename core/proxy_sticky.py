"""
Sticky session por conta — força mesmo IP de proxy entre requests da mesma @.

Por que: proxies rotativos (DataImpulse, BrightData, etc. no modo default)
dão um IP diferente CADA REQUEST. Instagram detecta:
  login → IP X
  verify session → IP Y
  get feed → IP Z
e marca como bot imediatamente (sessão pulando de IP = obviamente bot).

Solução: injetar um "session ID" único POR CONTA na URL do proxy. O
provedor mantém a mesma sub-rota física pra esse ID por X minutos.
Resultado: cada conta sai de UM IP fixo, contas diferentes saem de IPs
diferentes (continua isolando entre contas).

Formato varia por provedor:
  DataImpulse: ...username__cr.BR__sid.<ID>:pass
  Smartproxy:  ...user-session-<ID>-country-us:pass
  IPRoyal:     ...user_session-<ID>:pass
  BrightData:  ...user-session-<ID>:pass
  Oxylabs:     ...customer-USER-session-<ID>-country-XX:pass
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional
from urllib.parse import urlparse, urlunparse, quote


# Mapping hostname → função que sabe injetar sticky id no username
def _sticky_dataimpulse(user: str, sid: str) -> str:
    """DataImpulse: 'token__cr.BR' → 'token__cr.BR__sid.<sid>'"""
    if "__sid." in user.lower():
        return user  # já tem
    return f"{user}__sid.{sid}"


def _sticky_session_suffix(user: str, sid: str) -> str:
    """Smartproxy/IPRoyal/BrightData: 'user' → 'user-session-<sid>'.
    Se já tem -session-, não duplica."""
    if "-session-" in user.lower() or "_session-" in user.lower():
        return user
    return f"{user}-session-{sid}"


# (substrings de hostname, função de injection)
_PROVIDER_HANDLERS = [
    (("dataimpulse",), _sticky_dataimpulse),
    (("smartproxy", "smart-proxy", "smartdc"), _sticky_session_suffix),
    (("brd.superproxy.io", "brightdata", "luminati"), _sticky_session_suffix),
    (("iproyal", "geo.iproyal"), _sticky_session_suffix),
    (("oxylabs",), _sticky_session_suffix),
    (("proxyrack", "premium.proxyrack"), _sticky_session_suffix),
    (("proxyempire",), _sticky_session_suffix),
]


def _sid_from_username(account_username: str) -> str:
    """SID determinístico por conta — 12 hex chars do sha1(username)."""
    return hashlib.sha1(account_username.lower().encode("utf-8")).hexdigest()[:12]


def make_sticky(proxy_url: Optional[str], account_username: Optional[str]) -> Optional[str]:
    """Retorna proxy_url com sticky session id injetado, se possível.

    Se não conhecer o provedor (hostname não casa com nenhum), devolve
    o proxy original sem mexer — assim o user que tem proxy próprio
    custom não é afetado.

    Idempotente: se já tem session, devolve sem duplicar.
    """
    if not proxy_url or not account_username:
        return proxy_url
    try:
        p = urlparse(proxy_url)
        if not p.hostname or not p.username:
            return proxy_url
        host_lower = p.hostname.lower()
        handler = None
        for substrings, fn in _PROVIDER_HANDLERS:
            if any(s in host_lower for s in substrings):
                handler = fn
                break
        if handler is None:
            return proxy_url  # provedor desconhecido — não mexe
        sid = _sid_from_username(account_username)
        new_user = handler(p.username, sid)
        if new_user == p.username:
            return proxy_url  # nada mudou
        # Re-monta URL com o novo username (preserva password, host, port, etc)
        encoded_user = quote(new_user, safe="._-~")
        encoded_pass = quote(p.password, safe="._-~") if p.password else ""
        auth = f"{encoded_user}:{encoded_pass}" if encoded_pass else encoded_user
        netloc = f"{auth}@{p.hostname}"
        if p.port:
            netloc += f":{p.port}"
        return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))
    except Exception:
        return proxy_url


def detect_provider(proxy_url: Optional[str]) -> Optional[str]:
    """Identifica o provedor pelo hostname. Retorna nome legível ou None."""
    if not proxy_url:
        return None
    try:
        host = (urlparse(proxy_url).hostname or "").lower()
    except Exception:
        return None
    if "dataimpulse" in host:
        return "DataImpulse"
    if "smartproxy" in host or "smartdc" in host:
        return "Smartproxy"
    if "brd.superproxy" in host or "brightdata" in host or "luminati" in host:
        return "BrightData"
    if "iproyal" in host:
        return "IPRoyal"
    if "oxylabs" in host:
        return "Oxylabs"
    if "proxyrack" in host:
        return "ProxyRack"
    if "proxyempire" in host:
        return "ProxyEmpire"
    return None
