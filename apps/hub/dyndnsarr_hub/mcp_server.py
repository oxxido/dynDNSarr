"""Puente MCP stdio → API de servicios. Sin acceso a BD ni autoridad adicional."""

import asyncio
import json
import sys
from urllib.parse import urlsplit

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from dyndnsarr_hub import __version__
from dyndnsarr_hub.config import Settings
from dyndnsarr_shared.outbound import validate_url
from dyndnsarr_shared.tls import build_ssl_context


class Bridge:
    def __init__(self, http, url, token):
        self.http, self.url, self.token = http, url, token

    async def request(self, method, path, body=None):
        try:
            async with self.http.stream(
                method,
                self.url + path,
                json=body,
                headers={"Authorization": "Bearer " + self.token},
                follow_redirects=False,
                timeout=15,
            ) as response:
                if response.status_code != 200:
                    return {"code": "service_rejected", "status": response.status_code}
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 1024 * 1024:
                        return {"code": "response_too_large"}
                    chunks.append(chunk)
                return json.loads(b"".join(chunks))
        except (httpx.HTTPError, ValueError, OSError):
            return {"code": "service_unavailable"}


def create_server(bridge):
    server = Server("DynDNSarr", version=__version__)

    @server.list_tools()
    async def tools():
        result = await bridge.request("GET", "/service/v1/tools")
        return [
            types.Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
                annotations=types.ToolAnnotations(readOnlyHint=t["readOnly"], openWorldHint=True),
            )
            for t in result.get("tools", [])
        ]

    @server.call_tool()
    async def call(name, arguments):
        # No concatenar un nombre recibido a una ruta sin validarlo contra el catálogo.
        from dyndnsarr_hub.services.service_models import TOOLS

        if name not in TOOLS:
            result = {"code": "unknown_tool"}
        else:
            result = await bridge.request("POST", "/service/v1/tools/" + name, arguments)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result))],
            isError="code" in result,
        )

    return server


async def run(settings):
    url = validate_url(settings.mcp_hub_url, allow_http=True).rstrip("/")
    parts = urlsplit(url)
    if (
        parts.path
        or parts.query
        or (parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"))
        or not settings.mcp_token
    ):
        raise ValueError("Configura MCP_HUB_URL (HTTPS) y MCP_TOKEN")
    async with httpx.AsyncClient(
        verify=build_ssl_context(settings.mcp_ca_file), trust_env=False
    ) as http:
        server = create_server(Bridge(http, url, settings.mcp_token.get_secret_value()))
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


def cli():
    try:
        asyncio.run(run(Settings()))
    except (ValueError, OSError):
        # stdout se reserva exclusivamente al protocolo. Nunca mostrar valores de entorno.
        print(
            "Configuración MCP inválida; revisa MCP_HUB_URL, MCP_TOKEN y MCP_CA_FILE",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    cli()
