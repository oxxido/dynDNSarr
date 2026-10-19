import hashlib
import secrets

TOKEN_PREFIX = "dda_"
API_TOKEN_PREFIX = "ddh_"


def generate_token() -> str:
    """Token nuevo de un agente. Se muestra una vez; el Hub solo guarda su hash."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """`sha256` en hex. El token ya tiene 256 bits de entropía: un hash lento no aporta nada."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_api_token() -> str:
    """Token con el que el Hub llama a la API de un Agent (sentido Hub → Agent). Distinto del
    de `generate_token`; el Hub lo guarda en claro porque debe presentarlo (ADR-0006)."""
    return API_TOKEN_PREFIX + secrets.token_urlsafe(32)
