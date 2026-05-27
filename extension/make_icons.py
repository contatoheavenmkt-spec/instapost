"""Gera ícones PNG do Insta Poster pra extensão Chrome.

Roda 1x: `python make_icons.py` — cria icons/icon-16.png, 48.png, 128.png.
Não precisa de Pillow — usa só stdlib (zlib + struct + base64).

Design: gradient rosa→roxo com letra "IP" branca centralizada.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path


def write_png(path: Path, width: int, height: int, pixels_rgba: bytes) -> None:
    """Escreve um PNG cru a partir de bytes RGBA. Sem deps externas."""
    assert len(pixels_rgba) == width * height * 4

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xffffffff
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    # IDAT: prefixa cada linha com filter byte 0 (None)
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw.extend(pixels_rgba[y * stride:(y + 1) * stride])
    idat = zlib.compress(bytes(raw), 9)

    png = signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    path.write_bytes(png)


def make_icon(size: int) -> bytes:
    """Gera RGBA do ícone — gradient rose→violet diagonal + 'IP' branco."""
    pixels = bytearray(size * size * 4)

    # Cores do gradient (rose-500 → magenta → violet-500)
    # Linear: rosa #ec4899 → magenta #db2777 → violet #8b5cf6
    def lerp(a, b, t):
        return int(a + (b - a) * t)

    color_a = (0xec, 0x48, 0x99)  # pink-500
    color_b = (0xdb, 0x27, 0x77)  # pink-600
    color_c = (0x8b, 0x5c, 0xf6)  # violet-500

    # Letras "IP" como bitmap simples (5x7 por letra). Pra ícones pequenos
    # (16x16) só fica visível um quadrado colorido — suficiente.
    # Pra ≥48 desenhamos as letras.
    letter_I = [
        "11111",
        "00100",
        "00100",
        "00100",
        "00100",
        "00100",
        "11111",
    ]
    letter_P = [
        "11110",
        "10001",
        "10001",
        "11110",
        "10000",
        "10000",
        "10000",
    ]

    # Desenha "IP" centralizado se size >= 48
    draw_letters = size >= 48
    if draw_letters:
        # Cada letra 5x7, gap 1 = total 11x7. Escala pra cabe ~50% do ícone.
        scale = max(2, size // 18)
        text_w = (5 + 1 + 5) * scale
        text_h = 7 * scale
        text_x0 = (size - text_w) // 2
        text_y0 = (size - text_h) // 2

    def in_letter(x: int, y: int) -> bool:
        if not draw_letters:
            return False
        if x < text_x0 or y < text_y0 or x >= text_x0 + (5 + 1 + 5) * scale or y >= text_y0 + 7 * scale:
            return False
        rx = (x - text_x0) // scale
        ry = (y - text_y0) // scale
        if ry < 0 or ry >= 7:
            return False
        if rx < 5:
            return letter_I[ry][rx] == "1"
        if rx == 5:
            return False  # gap
        rx -= 6
        if 0 <= rx < 5:
            return letter_P[ry][rx] == "1"
        return False

    for y in range(size):
        for x in range(size):
            # t1 = diagonal (0 no canto sup-esq, 1 no canto inf-dir)
            t1 = (x + y) / (2 * (size - 1)) if size > 1 else 0
            # 2-stop: 0→0.5 rosa→magenta, 0.5→1 magenta→violet
            if t1 < 0.5:
                k = t1 * 2
                r = lerp(color_a[0], color_b[0], k)
                g = lerp(color_a[1], color_b[1], k)
                b = lerp(color_a[2], color_b[2], k)
            else:
                k = (t1 - 0.5) * 2
                r = lerp(color_b[0], color_c[0], k)
                g = lerp(color_b[1], color_c[1], k)
                b = lerp(color_b[2], color_c[2], k)

            # Letras "IP" em branco com sombra sutil
            if in_letter(x, y):
                r, g, b = 255, 255, 255

            # Borda arredondada: corta cantos (radius ≈ 20% do size)
            radius = max(2, size // 5)
            corner = False
            cx = min(x, size - 1 - x)
            cy = min(y, size - 1 - y)
            if cx < radius and cy < radius:
                dx = radius - cx
                dy = radius - cy
                if dx * dx + dy * dy > radius * radius:
                    corner = True

            alpha = 0 if corner else 255
            idx = (y * size + x) * 4
            pixels[idx] = r
            pixels[idx + 1] = g
            pixels[idx + 2] = b
            pixels[idx + 3] = alpha

    return bytes(pixels)


def main() -> None:
    out_dir = Path(__file__).parent / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)
    for size in (16, 48, 128):
        path = out_dir / f"icon-{size}.png"
        write_png(path, size, size, make_icon(size))
        print(f"escrito: {path}")


if __name__ == "__main__":
    main()
