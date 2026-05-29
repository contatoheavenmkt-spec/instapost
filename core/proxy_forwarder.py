"""
Proxy forwarder local: Chrome conecta em 127.0.0.1:PORT (sem auth),
forwarder autentica com o upstream (DataImpulse, BrightData, etc).

Usa asyncio pra suportar dezenas de conexões simultâneas (Chrome
abre ~30 conexões paralelas pra carregar uma página).
"""
import asyncio
import base64
import socket
import threading
from urllib.parse import urlparse


def _get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _handle_client(reader, writer, upstream_host, upstream_port, upstream_auth):
    """Trata uma conexão do Chrome."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=30)
        if not request_line:
            writer.close()
            return

        request_str = request_line.decode("utf-8", errors="ignore").strip()

        # Lê headers
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not line or line == b"\r\n":
                break

        if request_str.startswith("CONNECT"):
            target = request_str.split(" ")[1]
            await _handle_connect(writer, reader, target, upstream_host, upstream_port, upstream_auth)
        else:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()
    except Exception:
        try:
            writer.close()
        except Exception:
            pass


async def _handle_connect(client_writer, client_reader, target, upstream_host, upstream_port, upstream_auth):
    """Faz CONNECT tunnel via upstream proxy."""
    up_writer = None
    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(upstream_host, upstream_port),
            timeout=30,
        )

        connect_req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
        if upstream_auth:
            auth_b64 = base64.b64encode(upstream_auth.encode()).decode()
            connect_req += f"Proxy-Authorization: Basic {auth_b64}\r\n"
        connect_req += "\r\n"
        up_writer.write(connect_req.encode())
        await up_writer.drain()

        response_line = await asyncio.wait_for(up_reader.readline(), timeout=30)
        while True:
            line = await asyncio.wait_for(up_reader.readline(), timeout=10)
            if not line or line == b"\r\n":
                break

        if b"200" in response_line:
            client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_writer.drain()
            await asyncio.gather(
                _relay(client_reader, up_writer),
                _relay(up_reader, client_writer),
            )
        else:
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
    except Exception:
        try:
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
        except Exception:
            pass
    finally:
        for w in (client_writer, up_writer):
            if w:
                try:
                    w.close()
                except Exception:
                    pass


async def _relay(reader, writer):
    """Relay data de reader pra writer até EOF."""
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), timeout=300)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass


async def _run_server(host, port, upstream_host, upstream_port, upstream_auth):
    async def handler(reader, writer):
        await _handle_client(reader, writer, upstream_host, upstream_port, upstream_auth)
    server = await asyncio.start_server(handler, host, port)
    async with server:
        await server.serve_forever()


def start_forwarder(proxy_url: str) -> tuple:
    """Inicia proxy forwarder async em thread separada. Suporta centenas de conexões."""
    parsed = urlparse(proxy_url)
    upstream_host = parsed.hostname
    upstream_port = parsed.port or 8080
    upstream_auth = ""
    if parsed.username:
        upstream_auth = f"{parsed.username}:{parsed.password or ''}"

    port = _get_free_port()

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run_server("127.0.0.1", port, upstream_host, upstream_port, upstream_auth))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return None, port
