"""
Posta todas as mídias pendentes em todas as contas ativas.

Suporta:
  - Reels (mp4, default)
  - Stories (mp4 ou jpg/png) com link sticker opcional

O tipo e link de cada mídia ficam num .meta.json ao lado:
  content/pending/foo.mp4
  content/pending/foo.txt          ← legenda
  content/pending/foo.mp4.meta.json ← {"kind": "story", "link_url": "https://..."}

Sem .meta.json: mp4 vira Reel, jpg/png vira Story sem link.

Quando o disparo é Story com link, o sistema gera 1 URL curta DIFERENTE
por conta (via web/shortener) — todas apontam pro mesmo destino, evitando
cluster pelo Instagram.

Uso:
    python post.py                  # posta tudo
    python post.py --conta conta1   # posta só em uma conta
    python post.py --video x.mp4    # posta só uma mídia (mp4/jpg/png)
    python post.py --dry-run        # simula sem postar
"""
import argparse
import os
import random
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from core.session import get_client, load_accounts
from core.poster import (
    post_reel,
    post_story_photo,
    post_story_video,
    load_caption,
    load_meta,
    detect_media_kind,
)
from core.paths import PENDING_DIR, POSTED_DIR, LOGS_DIR

# Jitter entre postagens em contas diferentes (segundos)
MIN_DELAY_BETWEEN_ACCOUNTS = 60
MAX_DELAY_BETWEEN_ACCOUNTS = 180

# Jitter entre mídias diferentes na mesma rodada
MIN_DELAY_BETWEEN_VIDEOS = 300   # 5 min
MAX_DELAY_BETWEEN_VIDEOS = 900   # 15 min

# Extensões aceitas pra mídia
MEDIA_EXTS = (".mp4", ".jpg", ".jpeg", ".png", ".webp")


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_pending_media():
    """Lista mídias prontas pra postar (com .txt de legenda)."""
    items = []
    for media in PENDING_DIR.iterdir():
        if not media.is_file():
            continue
        if media.suffix.lower() not in MEDIA_EXTS:
            continue
        # Pula thumbs (.jpg gerado ao lado de .mp4)
        if media.suffix.lower() == ".jpg" and media.with_suffix(".mp4").exists():
            continue
        # Pula arquivos auxiliares
        if media.name.endswith(".meta.json"):
            continue
        txt = media.with_suffix(".txt")
        if txt.exists() or media.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            items.append(media)
        else:
            log(f"⚠️  {media.name} sem legenda (.txt), pulando")
    return sorted(items)


def archive_media(media: Path):
    """Move mídia postada pra pasta posted/ com seus auxiliares."""
    POSTED_DIR.mkdir(exist_ok=True)
    shutil.move(str(media), POSTED_DIR / media.name)
    # Auxiliares: .txt, .jpg (thumb pra mp4), .mp4.meta.json
    for sib_name in [
        media.stem + ".txt",
        media.stem + ".jpg",       # thumb pra mp4
        media.name + ".meta.json",
    ]:
        sib = PENDING_DIR / sib_name
        if sib.exists():
            shutil.move(str(sib), POSTED_DIR / sib_name)


def maybe_shorten_for_account(target_url: str, username: str, parent_slug_cache: dict, created_by: str = "post.py") -> str:
    """Se PYTHONPATH inclui web/, usa o shortener pra gerar URL curta por conta.
    Senão, retorna o target original.
    parent_slug_cache: dict compartilhado entre contas pro mesmo target, agrupa por parent."""
    try:
        from web.shortener import manager as link_manager
    except Exception:
        return target_url
    try:
        parent = parent_slug_cache.get(target_url)
        # Cria link único pra essa conta
        link = link_manager.create(
            target_url=target_url,
            label=f"story · @{username}",
            account=username,
            parent_slug=parent,
            created_by=created_by,
        )
        # Guarda parent_slug do primeiro link gerado pra agrupar os demais da mesma URL
        if not parent:
            # Usa slug do primeiro como parent_slug dos próximos
            parent_slug_cache[target_url] = link.slug
        # Monta URL pública: tenta variável de ambiente, senão usa localhost (dev only)
        base = os.environ.get("PUBLIC_BASE_URL") or "http://127.0.0.1:8000"
        return f"{base.rstrip('/')}/r/{link.slug}"
    except Exception as e:
        log(f"    ⚠ shortener falhou ({e}), usando URL original")
        return target_url


def post_to_account(cl, media: Path, caption: str, meta: dict, username: str, parent_slug_cache: dict):
    """Dispatch pra função correta baseado em kind e tipo de arquivo."""
    kind = meta.get("kind", "reel")
    link_url = meta.get("link_url")
    media_kind = detect_media_kind(str(media))

    # Story: tenta encurtar URL se houver
    if kind == "story" and link_url:
        link_url = maybe_shorten_for_account(link_url, username, parent_slug_cache)
        log(f"    🔗 link curto: {link_url}")

    if kind == "story":
        if media_kind == "photo":
            return post_story_photo(cl, str(media), caption, link_url)
        else:
            return post_story_video(cl, str(media), caption, link_url)
    else:
        # Reel só aceita vídeo
        if media_kind == "photo":
            return {"success": False, "media_id": None, "error": "Foto não pode virar Reel (use Story)"}
        return post_reel(cl, str(media), caption)


def main():
    # Força UTF-8 no stdout (Windows usa cp1252)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--conta", help="Postar só nesta conta")
    parser.add_argument("--video", help="Postar só esta mídia (nome do arquivo)")
    parser.add_argument("--dry-run", action="store_true", help="Simula sem postar")
    args = parser.parse_args()

    accounts = load_accounts()
    if args.conta:
        accounts = [a for a in accounts if a["username"] == args.conta]

    medias = get_pending_media()
    if args.video:
        medias = [m for m in medias if m.name == args.video]

    if not accounts:
        log("❌ Nenhuma conta ativa")
        return
    if not medias:
        log("❌ Nenhuma mídia em content/pending/")
        return

    log(f"🚀 Vai postar {len(medias)} mídia(s) em {len(accounts)} conta(s)")
    log(f"   Total de postagens: {len(medias) * len(accounts)}")

    if args.dry_run:
        log("[DRY RUN] Nenhuma postagem será feita")
        for m in medias:
            meta = load_meta(str(m))
            kind = meta.get("kind", "reel")
            link = f" + link {meta.get('link_url')}" if meta.get("link_url") else ""
            for a in accounts:
                log(f"  - [{kind}] {m.name}{link} → @{a['username']}")
        return

    for vi, media in enumerate(medias):
        caption = load_caption(str(media))
        meta = load_meta(str(media))
        kind = meta.get("kind", "reel")
        link_url = meta.get("link_url")
        log(f"\n📹 Mídia {vi+1}/{len(medias)}: [{kind}] {media.name}{' + link' if link_url else ''}")

        shuffled = accounts.copy()
        random.shuffle(shuffled)

        # Cache de parent_slug por target_url pra agrupar links gerados nessa rodada
        parent_slug_cache: dict = {}

        success_count = 0
        for ai, account in enumerate(shuffled):
            username = account["username"]
            log(f"  → @{username} ({ai+1}/{len(shuffled)})")

            try:
                cl = get_client(
                    username=username,
                    password=account["password"],
                    proxy=account.get("proxy"),
                    totp_secret=account.get("totp_secret"),
                )

                result = post_to_account(cl, media, caption, meta, username, parent_slug_cache)

                if result["success"]:
                    log(f"    ✅ Postado! media_id={result['media_id']}")
                    success_count += 1
                else:
                    log(f"    ❌ Falhou: {result['error']}")

            except Exception as e:
                log(f"    ❌ Erro: {e}")

            if ai < len(shuffled) - 1:
                delay = random.randint(MIN_DELAY_BETWEEN_ACCOUNTS, MAX_DELAY_BETWEEN_ACCOUNTS)
                log(f"    💤 Aguardando {delay}s antes da próxima conta...")
                time.sleep(delay)

        log(f"\n  Resultado: {success_count}/{len(accounts)} contas OK")

        if success_count > 0:
            archive_media(media)
            log(f"  📦 Mídia arquivada em content/posted/")

        if vi < len(medias) - 1:
            delay = random.randint(MIN_DELAY_BETWEEN_VIDEOS, MAX_DELAY_BETWEEN_VIDEOS)
            log(f"\n💤 Aguardando {delay}s antes da próxima mídia...")
            time.sleep(delay)

    log("\n🏁 Finalizado")


if __name__ == "__main__":
    main()
