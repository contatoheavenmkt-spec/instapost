"""
Entry point do servidor web.

Uso:
    venv\\Scripts\\activate
    python run.py            # padrão: http://localhost:8000
    python run.py --port 9000
    python run.py --host 127.0.0.1   # só localhost (default é 0.0.0.0 = acessível na rede local)
"""
import argparse
import socket
import sys

import uvicorn

# Força UTF-8 no stdout/stderr (Windows usa cp1252 por default e quebra com emoji)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0", help="IP de bind (0.0.0.0 = todas as interfaces)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="Hot-reload (dev)")
    args = parser.parse_args()

    ip = local_ip()
    print()
    print("=" * 60)
    print("  Insta Poster — Web UI")
    print("=" * 60)
    print(f"  Localhost:   http://127.0.0.1:{args.port}")
    if args.host == "0.0.0.0":
        print(f"  Rede local:  http://{ip}:{args.port}")
    print("=" * 60)
    print()

    uvicorn.run("web.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
