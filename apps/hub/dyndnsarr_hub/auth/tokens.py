import hashlib
import secrets

TOKEN_PREFIX = "dda_"


def generate_token() -> str:
    """Token nuevo de un agente. Se muestra una vez; el Hub solo guarda su hash."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """`sha256` en hex. El token ya tiene 256 bits de entropía: un hash lento no aporta nada."""
    return hashlib.sha256(token.encode()).hexdigest()
