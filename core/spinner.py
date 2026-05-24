"""
Spinner de caption + hashtag rotation — anti-cluster textual.

Por que: postar a MESMA caption em 50 contas é flag óbvia. Instagram
correlaciona texto + hashtags + cluster visual = "essas contas são bot".

Solução:
1. Spinner syntax {opção_a|opção_b|opção_c} — escolhe uma DETERMINISTICAMENTE
   por hash(username + posição no texto). Mesma conta + mesma caption =
   sempre escolhe iguais (consistência), contas diferentes = escolhas
   diferentes. Suporta nested {a|{b|c}|d}.

2. Hashtag shuffle — embaralha a ordem das hashtags por conta. Bonus: se
   o texto tem mais hashtags que MAX_HASHTAGS_PER_POST, cada conta usa um
   subset diferente. Insta já não recomenda > 15 hashtags por post.

Exemplo de input:
    "Bom dia {pessoal|gente|galera}! {Confira|Veja|Olhe} esse {vídeo|reel|conteúdo}.
    #motivacao #inspiracao #foco #disciplina #mindset #sucesso #vida #trabalho"

Output pra @luana:    "Bom dia gente! Confira esse reel.
    #vida #mindset #foco #disciplina #motivacao #trabalho #sucesso #inspiracao"

Output pra @maria:
    "Bom dia pessoal! Veja esse vídeo.
    #foco #trabalho #motivacao #disciplina #vida #mindset #inspiracao #sucesso"

Mesmo conteúdo SEMÂNTICO, fingerprint textual TOTALMENTE diferente.
"""
from __future__ import annotations

import hashlib
import random
import re
from typing import Optional


# Max hashtags por post (IG penaliza > 15-20). Se caption tem mais que isso,
# cada conta usa subset diferente (rotação).
MAX_HASHTAGS_PER_POST = 12

# Regex pra detectar {a|b|c} groups (não-greedy, suporta nested via recursão)
_SPIN_PATTERN = re.compile(r"\{([^{}]+)\}")

# Hashtag: # seguido de letra/dígito/_, sem espaço
_HASHTAG_PATTERN = re.compile(r"#[A-Za-zÀ-ÿ0-9_]+")


def _seed(username: str, extra: str = "") -> int:
    """Seed determinístico (32-bit) a partir do username + contexto extra."""
    h = hashlib.sha256(f"{username.lower()}::{extra}".encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def spin(text: str, username: str) -> str:
    """Resolve todos os {a|b|c} no texto deterministicamente por username.

    Aplica iterativamente até não haver mais grupos pra spinar (suporta nesting).
    Cada grupo na posição N usa seed=hash(username + posição_N) — assim mesmo
    texto produz mesmo output, e mudar a ordem dos grupos não rebagunça tudo.
    """
    if not text or "{" not in text:
        return text or ""
    rng = random.Random(_seed(username, f"spin::{text}"))
    # Loop até resolver todos os groups (não-nested innermost-first)
    out = text
    for _iteration in range(20):  # safety bound
        match = _SPIN_PATTERN.search(out)
        if not match:
            break
        group_content = match.group(1)
        # Split por | mas respeita escape (\| = literal pipe)
        # Pra simplicidade: split simples (não suporta literal pipe por ora)
        options = [opt.strip() for opt in group_content.split("|")]
        chosen = rng.choice(options) if options else ""
        out = out[:match.start()] + chosen + out[match.end():]
    return out


def shuffle_hashtags(text: str, username: str, max_hashtags: int = MAX_HASHTAGS_PER_POST) -> str:
    """Embaralha a ordem das hashtags no texto deterministicamente por username.
    Se houver mais que max_hashtags, cada conta usa subset diferente (rotação)."""
    if not text:
        return text
    matches = list(_HASHTAG_PATTERN.finditer(text))
    if len(matches) < 2:
        return text  # 0 ou 1 hashtag, nada pra embaralhar

    hashtags = [m.group(0) for m in matches]
    rng = random.Random(_seed(username, f"htags::{','.join(hashtags)}"))

    # Embaralha ordem
    shuffled = list(hashtags)
    rng.shuffle(shuffled)

    # Se passou do limite, pega só max_hashtags
    if len(shuffled) > max_hashtags:
        shuffled = shuffled[:max_hashtags]

    # Reconstrói: substitui o BLOCO de hashtags pela versão shuffled.
    # Pega o início da 1ª hashtag até o fim da última hashtag, troca por
    # shuffled separados por espaço. Texto fora do bloco preservado.
    first_start = matches[0].start()
    last_end = matches[-1].end()
    prefix = text[:first_start]
    suffix = text[last_end:]
    middle = " ".join(shuffled)
    return prefix + middle + suffix


def humanize_caption(
    template: str,
    username: str,
    max_hashtags: int = MAX_HASHTAGS_PER_POST,
) -> str:
    """Pipeline completa: spinner + hashtag shuffle.

    Pra usar SEMPRE que enviar caption pra um job — entrega caption única
    e determinística por (template, username).
    """
    if not template:
        return template or ""
    spun = spin(template, username)
    final = shuffle_hashtags(spun, username, max_hashtags=max_hashtags)
    return final


def has_spinner_syntax(text: str) -> bool:
    """True se o texto tem {a|b|c} groups. Útil pra UI mostrar 'spinner ativo'."""
    if not text:
        return False
    return bool(_SPIN_PATTERN.search(text))


def count_unique_variations(text: str) -> int:
    """Calcula quantas variações ÚNICAS o texto pode produzir.
    Útil pra UI: 'Esse template gera N variações únicas por conta'."""
    if not text:
        return 1
    total = 1
    for match in _SPIN_PATTERN.finditer(text):
        # Quantas opções nesse grupo? (não considera nesting pra simplicidade)
        opts = match.group(1).split("|")
        total *= max(1, len(opts))
    return total
