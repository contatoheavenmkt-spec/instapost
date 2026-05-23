"""
Criptografia simétrica das senhas/secrets em disco.

Por que: accounts.json tem password + totp_secret em plaintext. Se o disco
for comprometido (backup vazado, PC roubado, container hackeado), todas as
contas vazam de uma vez.

Solução: encrypt com Fernet (AES-128 CBC + HMAC SHA256). Chave gerada
automaticamente em DATA_DIR/.secret.key na 1ª execução. Migração transparente:
- 1ª carga vê plaintext → marca pra encrypt no próximo save
- Próximo save grava encrypted
- Loads subsequentes detectam prefix "enc:" e decifram

Reversível. Pra reverter (em dev), apaga `.secret.key` e refaz manual.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from core.paths import data_path

# Arquivo dedicado pro Fernet (separado de .secret.key que é usado pra
# session signing). Fernet exige 32 bytes url-safe base64 — não dá pra
# compartilhar com outras chaves.
CRYPT_KEY_FILE = data_path(".crypt.key")

_PREFIX = "enc:"
_fernet: Optional[Fernet] = None


def _ensure_key() -> bytes:
    """Carrega chave do disco ou gera nova (atomic-ish: write-rename)."""
    p = Path(str(CRYPT_KEY_FILE))
    if p.exists():
        try:
            data = p.read_bytes()
            if data:
                return data
        except Exception:
            pass
    # Gera nova chave
    key = Fernet.generate_key()
    tmp = p.with_suffix(".key.tmp")
    tmp.write_bytes(key)
    tmp.replace(p)
    # 0600 quando possível (POSIX)
    try:
        import os
        os.chmod(p, 0o600)
    except Exception:
        pass
    print(f"[credentials] nova chave gerada em {p}")
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_ensure_key())
    return _fernet


def encrypt(plain: Optional[str]) -> Optional[str]:
    """Encripta string. None/vazio passa direto. Strings já encriptadas (prefix
    "enc:") passam direto (idempotente)."""
    if not plain:
        return plain
    if plain.startswith(_PREFIX):
        return plain
    token = _get_fernet().encrypt(plain.encode("utf-8"))
    return _PREFIX + token.decode("ascii")


def decrypt(value: Optional[str]) -> Optional[str]:
    """Decripta string. Se não tem prefix "enc:", devolve como está (plaintext
    legado). Se decifragem falhar (chave perdida ou corrupção), devolve None
    com aviso — NÃO levanta exceção (evita travar o load de N contas por 1 corrompida)."""
    if not value:
        return value
    if not value.startswith(_PREFIX):
        return value  # plaintext legado, devolve como está
    try:
        encrypted = value[len(_PREFIX):].encode("ascii")
        return _get_fernet().decrypt(encrypted).decode("utf-8")
    except (InvalidToken, Exception) as e:
        print(f"[credentials] ⚠️ falha decifrando — chave perdida ou corrupção? {type(e).__name__}")
        return None


def is_encrypted(value: Optional[str]) -> bool:
    return bool(value) and value.startswith(_PREFIX)
