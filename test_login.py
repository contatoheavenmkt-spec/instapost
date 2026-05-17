"""
Testa login de UMA conta. Use ANTES de tentar postar em massa.

Uso:
    python test_login.py nome_da_conta

A primeira vez vai pedir verificação (challenge) provavelmente.
Você precisa olhar o email da conta e digitar o código.
Depois disso a sessão fica salva em sessions/nome_da_conta.json

Suporta UI interativa do web/jobs.py: quando o script chama input(),
imprime uma marca [AWAITING:...] que a UI detecta e mostra input box.
"""
import sys
from core.session import get_client, load_accounts


def make_challenge_handler(username: str):
    """Handler de challenge code do instagrapi.
    Chamado quando Instagram pede código por email/SMS.
    Imprime marca [AWAITING:...] pra UI detectar, depois lê de stdin."""

    def handler(_username, choice):
        # choice é o método (0=SMS, 1=email). instagrapi usa esse argumento.
        method = "SMS" if choice == 0 else "email"
        prompt = f"Instagram pediu código de verificação por {method}. Digite o código (6 dígitos):"
        # A marca [AWAITING:...] vira input na UI quando rodando via web/jobs.py
        print(f"[AWAITING:{prompt}]", flush=True)
        code = input().strip()
        print(f"  → código recebido, enviando…", flush=True)
        return code

    return handler


def make_totp_handler(username: str):
    """Fallback se a conta tem 2FA TOTP e a chave NÃO foi cadastrada:
    pede o código de 6 dígitos atual (ex: do 2fa.ac ou Google Authenticator)."""

    def handler():
        prompt = f"Conta @{username} tem 2FA. Cadastre a chave 2FA na UI pra automatizar, ou digite o código de 6 dígitos agora:"
        print(f"[AWAITING:{prompt}]", flush=True)
        return input().strip()

    return handler


def main():
    if len(sys.argv) < 2:
        print("Uso: python test_login.py <username>")
        print("\nContas disponíveis em accounts.json:")
        for acc in load_accounts():
            print(f"  - {acc['username']}")
        sys.exit(1)

    target = sys.argv[1]
    accounts = load_accounts()

    account = next((a for a in accounts if a["username"] == target), None)
    if not account:
        print(f"❌ Conta '{target}' não encontrada em accounts.json")
        sys.exit(1)

    print(f"Tentando logar como {target}...")
    try:
        cl = get_client(
            username=account["username"],
            password=account["password"],
            proxy=account.get("proxy"),
            totp_secret=account.get("totp_secret"),
            challenge_handler=make_challenge_handler(target),
            totp_fallback_handler=make_totp_handler(target),
        )
        # Pega info do perfil pra validar
        info = cl.account_info()
        print(f"\n✅ Login OK!")
        print(f"   Username: {info.username}")
        print(f"   Nome: {info.full_name}")
        # Stats públicas vêm de user_info, não account_info
        try:
            user = cl.user_info(cl.user_id)
            print(f"   Seguidores: {user.follower_count}")
            print(f"   Seguindo: {user.following_count}")
            print(f"   Posts: {user.media_count}")
        except Exception:
            pass
        print(f"   Sessão salva em: sessions/{target}.json")

    except Exception as e:
        print(f"\n❌ Falhou: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
