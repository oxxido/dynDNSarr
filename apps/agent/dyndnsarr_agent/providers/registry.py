import httpx

from dyndnsarr_agent.config import Settings
from dyndnsarr_agent.ddns.config import DdnsConfig
from dyndnsarr_agent.providers.base import DnsProvider
from dyndnsarr_agent.providers.dondominio import DonDominioProvider


def build_providers(
    config: DdnsConfig, settings: Settings, client: httpx.AsyncClient
) -> dict[str, DnsProvider]:
    """Proveedores que usan los registros `managed` habilitados (vacío si no hay ninguno).

    Las credenciales ya se comprobaron al cargar la configuración (`require_provider_credentials`).
    """
    names = {r.provider for r in config.records if r.mode == "managed" and r.enabled}
    providers: dict[str, DnsProvider] = {}
    if "dondominio" in names:
        providers["dondominio"] = DonDominioProvider(
            client,
            config.providers.dondominio.url,
            settings.dondominio_user,
            settings.dondominio_key,
        )
    return providers
