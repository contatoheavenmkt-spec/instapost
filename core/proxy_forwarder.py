"""
Proxy forwarder local: Chrome conecta em 127.0.0.1:PORT (sem auth),
forwarder autentica com o upstream (DataImpulse, BrightData, etc).
"""
import threading
import socket
import select
import base64
from urllib.parse import urlparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn


def _get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ProxyHandler(BaseHTTPRequestHandler):
    upstream_host = ""
    upstream_port = 0
    upstream_auth = ""

    def log_message(self, format, *args):
        pass

    def do_CONNECT(self):
        try:
            upstream = socket.create_connection(
                (self.upstream_host, self.upstream_port), timeout=60
            )
            connect_line = f"CONNECT {self.path} HTTP/1.1\r\n"
            hdrs = f"Host: {self.path}\r\n"
            if self.upstream_auth:
                auth_b64 = base64.b64encode(self.upstream_auth.encode()).decode()
                hdrs += f"Proxy-Authorization: Basic {auth_b64}\r\n"
            hdrs += "\r\n"
            upstream.sendall((connect_line + hdrs).encode())

            response = b""
            while b"\r\n\r\n" not in response:
                chunk = upstream.recv(4096)
                if not chunk:
                    break
                response += chunk

            if b"200" in response.split(b"\r\n")[0]:
                self.send_response(200, "Connection Established")
                self.end_headers()
                self._tunnel(self.connection, upstream)
            else:
                self.send_response(502)
                self.end_headers()
                upstream.close()
        except Exception:
            try:
                self.send_response(502)
                self.end_headers()
            except Exception:
                pass

    def _tunnel(self, client, upstream):
        try:
            sockets = [client, upstream]
            while True:
                readable, _, errors = select.select(sockets, [], sockets, 120)
                if errors:
                    break
                for s in readable:
                    data = s.recv(65536)
                    if not data:
                        return
                    if s is client:
                        upstream.sendall(data)
                    else:
                        client.sendall(data)
        except Exception:
            pass
        finally:
            try:
                upstream.close()
            except Exception:
                pass


def start_forwarder(proxy_url: str) -> tuple:
    parsed = urlparse(proxy_url)
    upstream_host = parsed.hostname
    upstream_port = parsed.port or 8080
    upstream_auth = ""
    if parsed.username:
        upstream_auth = f"{parsed.username}:{parsed.password or ''}"

    port = _get_free_port()

    handler = type("Handler", (_ProxyHandler,), {
        "upstream_host": upstream_host,
        "upstream_port": upstream_port,
        "upstream_auth": upstream_auth,
    })

    class ThreadedProxy(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedProxy(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    return server, port
