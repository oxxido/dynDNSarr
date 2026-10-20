"""Proxy HTTPS local usado solo por los e2e; el upstream HTTP es loopback."""

import os

import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "HEAD", "OPTIONS"])
async def proxy(path: str, request: Request):
    headers = {k: v for k, v in request.headers.items() if k not in ("host", "connection")}
    async with httpx.AsyncClient() as client:
        reply = await client.request(
            request.method,
            os.environ["TEST_HUB_UPSTREAM"] + "/" + path,
            params=request.query_params,
            headers=headers,
            content=await request.body(),
        )
    response = Response(reply.content, status_code=reply.status_code)
    response.raw_headers = [
        (k, v)
        for k, v in reply.headers.raw
        if k.lower() not in (b"connection", b"transfer-encoding")
    ]
    return response
