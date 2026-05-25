"""
Proxy HTTP forwarder local — bypassa o auth dialog do Chrome.

Por que: Chrome com --proxy-server=http://user:pass@host:port REJEITA
o user:pass na URL. Em vez disso, popa um dialog pedindo credenciais
em cada navegação. Tentamos via extensão (MV2 deprecated, MV3 com
race condition), nada confiável.

Solução: rodar um mini-proxy local em 127.0.0.1:PORT (sem auth no
nível do Chrome) que aceita conexões do Chrome e ENCAMINHA pro proxy
upstream (DataImpulse) já com o header Proxy-Authorization correto.

Resultado: Chrome conecta em localhost (sem auth dialog), forwarder
adiciona auth ao falar com upstream. 100% transparente.

Performance: suporta CONNECT (HTTPS, cobre ~95% do tráfego web) +
GET/POST simples. Cada conexão é uma thread (não escala mil clientes
mas pra 1 Chrome basta sobrar).
"""
from __future__ import annotations

import base64
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse


def _parse_upstream(proxy_url: str) -> tuple[str, int, Optional[str], Optional[str]]:
    """Quebra a URL do proxy upstream em host, port, user, pass."""
    p = urlparse(proxy_url)
    host = p.hostname or ""
    port = p.port or (1080 if "socks" in (p.scheme or "").lower() else 80)
    user = p.username
    password = p.password
    return host, port, user, password


def _make_auth_header(user: Optional[str], password: Optional[str]) -> Optional[str]:
    if not user:
        return None
    token = base64.b64encode(f"{user}:{password or ''}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


class _Forwarder(BaseHTTPRequestHandler):
    """Cada classe-instância tem upstream_* setados via subclass dinâmico."""
    upstream_host: str = ""
    upstream_port: int = 0
    upstream_auth: Optional[str] = None
    timeout = 60

    def log_message(self, format, *args):
        # Silencia logs default do http.server
        pass

    def do_CONNECT(self):
        """HTTPS via tunnel. self.path = 'host:port' destino final."""
        try:
            upstream = socket.create_connection(
                (self.upstream_host, self.upstream_port), timeout=15
            )
        except Exception as e:
            self._send_error(502, f"upstream connect failed: {e}")
            return

        try:
            # Pede CONNECT pro upstream com auth
            connect_req = f"CONNECT {self.path} HTTP/1.1\r\nHost: {self.path}\r\n"
            if self.upstream_auth:
                connect_req += f"Proxy-Authorization: {self.upstream_auth}\r\n"
            connect_req += "\r\n"
            upstream.sendall(connect_req.encode("ascii"))

            # Lê resposta do upstream (headers + linha em branco)
            response = b""
            upstream.settimeout(15)
            while b"\r\n\r\n" not in response:
                chunk = upstream.recv(4096)
                if not chunk:
                    self._send_error(502, "upstream closed during CONNECT handshake")
                    upstream.close()
                    return
                response += chunk
                if len(response) > 65536:
                    self._send_error(502, "upstream response too large")
                    upstream.close()
                    return

            # Status do upstream
            status_line = response.split(b"\r\n", 1)[0]
            if b" 200 " not in status_line and b" 200\r" not in status_line:
                # Falha no upstream (auth ruim, etc) — propaga
                self.connection.sendall(response)
                upstream.close()
                return

            # Repassa resposta pra cliente (200 Connection Established)
            self.connection.sendall(response)

            # Inicia tunnel bidirecional
            self._tunnel(self.connection, upstream)
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def _tunnel(self, a: socket.socket, b: socket.socket) -> None:
        """Forward bytes A <-> B até qualquer um fechar."""
        a.settimeout(None)
        b.settimeout(None)
        sockets = [a, b]
        while True:
            try:
                r, _, _ = select.select(sockets, [], [], 300)
            except Exception:
                return
            if not r:
                return  # timeout sem atividade = fecha
            for s in r:
                try:
                    data = s.recv(8192)
                except Exception:
                    return
                if not data:
                    return
                other = b if s is a else a
                try:
                    other.sendall(data)
                except Exception:
                    return

    def _do_http_forward(self) -> None:
        """HTTP plain (não HTTPS). Forwarda request com auth header."""
        # Lê body se houver
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_length) if content_length > 0 else b""

        # Monta request pra upstream — usamos absolute URL no path (HTTP proxy style)
        try:
            upstream = socket.create_connection(
                (self.upstream_host, self.upstream_port), timeout=15
            )
        except Exception as e:
            self._send_error(502, f"upstream connect failed: {e}")
            return

        try:
            req = f"{self.command} {self.path} HTTP/1.1\r\n"
            # Headers do client (sem hop-by-hop)
            hop_by_hop = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}
            for k, v in self.headers.items():
                if k.lower() in hop_by_hop:
                    continue
                req += f"{k}: {v}\r\n"
            if self.upstream_auth:
                req += f"Proxy-Authorization: {self.upstream_auth}\r\n"
            req += "Connection: close\r\n\r\n"
            upstream.sendall(req.encode("iso-8859-1") + body)

            # Forward response
            upstream.settimeout(60)
            while True:
                chunk = upstream.recv(8192)
                if not chunk:
                    break
                try:
                    self.connection.sendall(chunk)
                except Exception:
                    break
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def do_GET(self):    self._do_http_forward()
    def do_POST(self):   self._do_http_forward()
    def do_PUT(self):    self._do_http_forward()
    def do_DELETE(self): self._do_http_forward()
    def do_HEAD(self):   self._do_http_forward()

    def _send_error(self, code: int, msg: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(msg.encode("utf-8"))
        except Exception:
            pass


def start_forwarder(upstream_proxy_url: str) -> tuple[ThreadingHTTPServer, int]:
    """Sobe forwarder local apontando pro upstream.
    Retorna (server, local_port). Server roda em thread daemon.
    Pra parar, chame server.shutdown()."""
    host, port, user, password = _parse_upstream(upstream_proxy_url)
    if not host or not port:
        raise ValueError(f"upstream proxy URL inválida: {upstream_proxy_url}")

    auth = _make_auth_header(user, password)

    # Cria subclass com upstream config (cada forwarder tem seu próprio)
    class _ConfiguredForwarder(_Forwarder):
        pass
    _ConfiguredForwarder.upstream_host = host
    _ConfiguredForwarder.upstream_port = port
    _ConfiguredForwarder.upstream_auth = auth

    server = ThreadingHTTPServer(("127.0.0.1", 0), _ConfiguredForwarder)
    server.daemon_threads = True
    local_port = server.server_port

    thread = threading.Thread(target=server.serve_forever, daemon=True, name=f"proxy-fwd-{local_port}")
    thread.start()

    return server, local_port
