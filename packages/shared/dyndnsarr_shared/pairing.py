"""Contrato `/pair/v1` de emparejamiento Agent ↔ Hub por código compartido (ADR-0013).

El código nunca viaja: cada lado demuestra conocerlo con un HMAC-SHA256. Los MAC llevan una
etiqueta distinta por mensaje, así que uno no sirve en lugar de otro.
"""

import hashlib
import hmac
import json
import re
import ssl

from pydantic import BaseModel, ConfigDict, Field, field_validator

from dyndnsarr_shared.events import AGENT_ID_PATTERN

# 128 bits como mínimo: `openssl rand -hex 16`.
MIN_CODE_HEX = 32
_CODE = re.compile(rf"^[0-9a-f]{{{MIN_CODE_HEX},128}}$")
NONCE_PATTERN = r"^[0-9a-f]{64}$"
MAC_PATTERN = r"^[0-9a-f]{64}$"
HUB_TOKEN_PATTERN = r"^dda_[A-Za-z0-9_-]{32,128}$"
API_TOKEN_PATTERN = r"^ddh_[A-Za-z0-9_-]{32,128}$"
MAX_CA_PEM = 8192

CHALLENGE_LABEL = "hub-ca"
CLAIM_LABEL = "agent-claim"
CLAIMED_LABEL = "hub-claimed"


def normalize_code(value: str) -> str:
    """Código en minúsculas y sin espacios. Lanza `ValueError` si tiene menos de 128 bits o no
    es hexadecimal: un código débil no debe permitir arrancar."""
    code = value.strip().lower()
    if not _CODE.match(code):
        raise ValueError(
            f"el código de emparejamiento debe ser hexadecimal de al menos {MIN_CODE_HEX} "
            "caracteres (128 bits): genera uno con `openssl rand -hex 16`"
        )
    return code


def code_hash(code: str) -> str:
    """Identifica el código sin guardarlo. Tiene ≥128 bits de entropía: basta un `sha256`."""
    return hashlib.sha256(("pairing-code:" + code).encode()).hexdigest()


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def mac(code: str, label: str, *parts: str) -> str:
    """HMAC-SHA256 en hex sobre la etiqueta y las partes en JSON: sin ambigüedad de separadores."""
    message = json.dumps([label, *parts], separators=(",", ":"), ensure_ascii=False)
    return hmac.new(code.encode(), message.encode(), hashlib.sha256).hexdigest()


def verify_mac(code: str, expected: str, label: str, *parts: str) -> bool:
    """Comparación en tiempo constante."""
    return hmac.compare_digest(mac(code, label, *parts).encode(), expected.encode())


def check_ca_pem(pem: str) -> str:
    """Una CA en PEM que `ssl` acepta como raíz de confianza; si no, `ValueError`."""
    if len(pem) > MAX_CA_PEM or "-----BEGIN CERTIFICATE-----" not in pem:
        raise ValueError("no es un certificado PEM")
    try:
        ssl.create_default_context(cadata=pem)
    except (ssl.SSLError, ValueError):
        raise ValueError("no es un certificado PEM válido") from None
    return pem


class ChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nonce: str = Field(pattern=NONCE_PATTERN)


class ChallengeResponse(BaseModel):
    """La CA del Hub y `mac(código, "hub-ca", nonce, sha256(ca_pem))`."""

    model_config = ConfigDict(extra="forbid")

    ca_pem: str = Field(max_length=MAX_CA_PEM)
    mac: str = Field(pattern=MAC_PATTERN)

    def mac_parts(self, nonce: str) -> tuple[str, ...]:
        return (nonce, sha256_hex(self.ca_pem))


class ClaimRequest(BaseModel):
    """Lo que el Agent entrega al Hub, ya por TLS verificado con la CA del desafío. Los tokens los
    genera el Agent: repetir el mismo canje es idempotente."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    api_url: str = Field(max_length=512)
    api_ca_pem: str = Field(max_length=MAX_CA_PEM)
    hub_token: str = Field(pattern=HUB_TOKEN_PATTERN)
    api_token: str = Field(pattern=API_TOKEN_PATTERN)
    mac: str = Field(pattern=MAC_PATTERN)

    @field_validator("api_ca_pem")
    @classmethod
    def _check_ca(cls, pem: str) -> str:
        return check_ca_pem(pem)

    def mac_parts(self) -> tuple[str, ...]:
        return (self.agent_id, self.api_url, self.api_ca_pem, self.hub_token, self.api_token)


class ClaimResponse(BaseModel):
    """`mac(código, "hub-claimed", agent_id, mac del canje)`."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    mac: str = Field(pattern=MAC_PATTERN)
