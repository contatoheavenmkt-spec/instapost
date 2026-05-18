"""
Funções de gestão de conta Instagram via instagrapi.
Edita perfil (foto, bio, nome, link) e executa automações conservadoras.
"""
from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Optional


def get_profile_info(cl) -> dict:
    """Retorna info atual do perfil pra mostrar no painel antes de editar."""
    try:
        info = cl.account_info()
        # user_info pega stats públicos
        try:
            user = cl.user_info(cl.user_id)
            return {
                "username": info.username,
                "full_name": info.full_name or "",
                "biography": user.biography or "",
                "external_url": user.external_url or "",
                "profile_pic_url": str(user.profile_pic_url or info.profile_pic_url or ""),
                "follower_count": user.follower_count,
                "following_count": user.following_count,
                "media_count": user.media_count,
                "is_private": bool(user.is_private),
                "is_verified": bool(user.is_verified),
            }
        except Exception:
            return {
                "username": info.username,
                "full_name": info.full_name or "",
                "biography": "",
                "external_url": "",
                "profile_pic_url": str(info.profile_pic_url or ""),
                "follower_count": 0,
                "following_count": 0,
                "media_count": 0,
                "is_private": False,
                "is_verified": False,
            }
    except Exception as e:
        return {"error": str(e)}


def edit_profile_info(cl, biography: Optional[str] = None,
                       full_name: Optional[str] = None,
                       external_url: Optional[str] = None) -> dict:
    """Atualiza bio, nome ou link da bio. Campos None = não alterar."""
    try:
        kwargs = {}
        if biography is not None:
            kwargs["biography"] = biography
        if full_name is not None:
            kwargs["full_name"] = full_name
        if external_url is not None:
            kwargs["external_url"] = external_url
        if not kwargs:
            return {"success": True, "info": "nenhum campo pra atualizar"}
        cl.account_edit(**kwargs)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def change_profile_picture(cl, image_path: str) -> dict:
    """Troca foto de perfil. image_path = caminho local pro arquivo (.jpg)."""
    try:
        p = Path(image_path)
        if not p.exists():
            return {"success": False, "error": f"arquivo não encontrado: {image_path}"}
        cl.account_change_picture(p)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ===================== AUTOMAÇÕES =====================

def auto_like_own_recent_comments(cl, max_likes: int = 5, recent_posts: int = 10) -> dict:
    """
    Curte comentários não-curtidos dos próprios posts recentes.
    - max_likes: total a curtir nesta execução
    - recent_posts: quantos posts próprios buscar comentários

    Retorna dict com count_liked + comments_seen pra log/telemetria.
    """
    try:
        liked_count = 0
        seen_count = 0
        my_medias = cl.user_medias(cl.user_id, amount=recent_posts)
        if not my_medias:
            return {"success": True, "liked": 0, "seen": 0, "note": "sem posts próprios"}

        # Embaralha ordem pra não focar sempre nos mesmos posts
        random.shuffle(my_medias)

        for media in my_medias:
            if liked_count >= max_likes:
                break
            try:
                comments = cl.media_comments(media.pk, amount=15)
            except Exception:
                continue
            seen_count += len(comments)
            random.shuffle(comments)
            for c in comments:
                if liked_count >= max_likes:
                    break
                # Pula comentários já curtidos ou próprios
                if getattr(c, "has_liked", False):
                    continue
                if str(getattr(c.user, "pk", "")) == str(cl.user_id):
                    continue
                try:
                    cl.comment_like(c.pk)
                    liked_count += 1
                    # Delay aleatório entre likes (anti-detecção)
                    time.sleep(random.uniform(8, 25))
                except Exception as e:
                    err = str(e).lower()
                    if "challenge" in err or "checkpoint" in err or "feedback_required" in err:
                        return {"success": False, "liked": liked_count, "seen": seen_count,
                                "error": "Instagram bloqueou — parando automação"}

        return {"success": True, "liked": liked_count, "seen": seen_count}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_latest_own_story_pk(cl) -> Optional[str]:
    """Retorna o pk do Story mais recente da conta (últimas 24h).
    Útil pra casos onde post_story_* retornou success mas sem media_id
    (phantom error) — fallback pra adicionar a destaque automaticamente."""
    try:
        # Sleep curto pra dar tempo do Story aparecer no feed após post
        import time as _t
        _t.sleep(2)
        # user_stories retorna lista de Story ativos
        stories = cl.user_stories(cl.user_id, amount=5)
        if not stories:
            return None
        # Mais recente primeiro
        try:
            latest = max(stories, key=lambda s: getattr(s, "taken_at", 0))
        except Exception:
            latest = stories[0]
        return str(latest.pk)
    except Exception as e:
        print(f"[latest_story] falhou: {e}")
        return None


def add_story_to_highlight(cl, story_pk, title: str) -> dict:
    """Adiciona um story (recém-postado) a um destaque (highlight).
    Se existe destaque com `title` (case-insensitive), adiciona o story.
    Senão, cria destaque novo com esse story como capa.

    Args:
        cl: Client instagrapi
        story_pk: pk da Story já postada
        title: nome do destaque (ex: 'Promoções', 'Cardápio')
    """
    if not title:
        return {"success": False, "error": "title vazio"}
    try:
        # Lista destaques atuais da conta
        highlights = cl.user_highlights(cl.user_id)
        title_norm = title.strip().lower()
        target = next(
            (h for h in highlights if (getattr(h, "title", "") or "").strip().lower() == title_norm),
            None,
        )
        if target:
            cl.highlight_add_stories(target.pk, [int(story_pk)])
            return {
                "success": True,
                "action": "added_to_existing",
                "highlight_pk": str(target.pk),
                "highlight_title": target.title,
            }
        else:
            h = cl.highlight_create(title=title.strip(), story_ids=[int(story_pk)])
            return {
                "success": True,
                "action": "created_new",
                "highlight_pk": str(h.pk),
                "highlight_title": h.title,
            }
    except Exception as e:
        return {"success": False, "error": str(e)}


def auto_follow_back_new_followers(cl, seen_followers: list,
                                     max_follows: int = 2) -> dict:
    """
    Detecta novos seguidores (não estão em seen_followers) e segue de volta.
    - seen_followers: lista de IDs já vistos antes (vem do cache da conta)
    - max_follows: máximo a seguir nesta execução

    Retorna dict com followed list + new seen_followers atualizado.
    """
    try:
        # Pega últimos seguidores (instagrapi limita a 1000 mas pegamos uns 200)
        current = cl.user_followers(cl.user_id, amount=200)
        if not current:
            return {"success": True, "followed": [], "seen_followers": seen_followers}

        current_ids = list(current.keys())  # dict {pk: user}

        # Quem é novo
        seen_set = set(str(x) for x in seen_followers)
        new_ids = [pk for pk in current_ids if str(pk) not in seen_set]

        # Se for a 1ª vez (seen vazio), só preenche o cache sem seguir ninguém
        if not seen_followers:
            return {
                "success": True,
                "followed": [],
                "seen_followers": [str(x) for x in current_ids],
                "note": "primeira execução — cache inicial, sem follows",
            }

        if not new_ids:
            return {
                "success": True,
                "followed": [],
                "seen_followers": [str(x) for x in current_ids],
            }

        # Segue até max_follows novos, com delay
        random.shuffle(new_ids)
        followed = []
        for pk in new_ids[:max_follows]:
            try:
                cl.user_follow(int(pk))
                user_obj = current.get(pk) or current.get(str(pk))
                followed.append({
                    "id": str(pk),
                    "username": getattr(user_obj, "username", "?"),
                })
                time.sleep(random.uniform(20, 60))
            except Exception as e:
                err = str(e).lower()
                if "challenge" in err or "checkpoint" in err or "feedback_required" in err:
                    return {"success": False, "followed": followed, "error": "Instagram bloqueou — parando"}

        return {
            "success": True,
            "followed": followed,
            "seen_followers": [str(x) for x in current_ids],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
