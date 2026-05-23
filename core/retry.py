"""
Retry com backoff exponencial + jitter pra erros de rate limit (429).

Por que: instagrapi às vezes recebe 429 ou "feedback_required: Please wait
a few minutes". Sem retry, cada job morre na 1ª tentativa, acumulando
erros e jogando o user pra modo "verificação" sem necessidade.

Estratégia: tenta N vezes com delay exponencial (2s, 4s, 8s, 16s...) +
jitter aleatório. Max delay 5min. Logamos cada tentativa.

Detecção do 429: substring no error_msg. Conservador — só retry quando
SÓ for rate limit, não em outros erros (challenge, banned, etc).
"""
from __future__ import annotations

import random
import time
from functools import wraps
from typing import Callable, TypeVar

T = TypeVar("T")


# Substrings (lowercase) que indicam "tente de novo daqui a pouco"
RATE_LIMIT_PATTERNS = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "wait a few minutes",
    "wait_a_few_minutes",
    "please wait",
    "please_wait",
    "try again later",
    "try_again_later",
    "temporarily blocked",
    "service unavailable",  # 503
)


def is_rate_limit_error(err: Exception) -> bool:
    msg = str(err).lower()
    return any(p in msg for p in RATE_LIMIT_PATTERNS)


def with_retry(
    fn: Callable[..., T],
    *args,
    max_retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 300.0,
    label: str = "",
    **kwargs,
) -> T:
    """Roda fn(*args, **kwargs) com retries em 429. Levanta exceção se
    não for rate limit ou se esgotou as tentativas."""
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not is_rate_limit_error(e) or attempt >= max_retries:
                raise
            # Expo backoff: 2s, 4s, 8s... + jitter [0, base_delay)
            delay = min(max_delay, base_delay * (2 ** attempt)) + random.uniform(0, base_delay)
            tag = f"[retry{':' + label if label else ''}]"
            print(f"{tag} 429/rate limit detectado — esperando {delay:.0f}s (tentativa {attempt + 1}/{max_retries})")
            time.sleep(delay)
            attempt += 1


def retry_on_429(max_retries: int = 3, base_delay: float = 2.0, max_delay: float = 300.0, label: str = ""):
    """Decorator wrapper de with_retry."""
    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            return with_retry(fn, *args, max_retries=max_retries, base_delay=base_delay,
                             max_delay=max_delay, label=label or fn.__name__, **kwargs)
        return wrapper
    return decorator


def humanlike_delay(min_s: int = 60, mean_s: int = 180, max_s: int = 600) -> int:
    """Delay com distribuição exponencial truncada — mais natural que uniforme.

    Usuário real tem padrão long-tail: a maioria das ações em <2min, mas uma
    minoria leva 10min+ (pausas, distrações). Insta tem detector de "muito regular"
    que random.randint(60, 180) bate de cheio (todos delays na faixa 60-180s linear).

    Distribuição: shifted exponential. Mean = mean_s. Min = min_s. Truncado em max_s.
    """
    span = max(1, mean_s - min_s)
    d = min_s + random.expovariate(1.0 / span)
    return int(min(max_s, d))
