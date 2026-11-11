#!/usr/bin/env python3
"""Read-only MCP initialize/list/status probe using the official Python SDK."""
import argparse
import asyncio
import json
import os
import ssl
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def probe(url):
    parsed = urlsplit(url)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.hostname or parsed.path != "/mcp"):
        raise ValueError("Use the exact /mcp URL without credentials/query/fragment.")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("HTTPS is required outside loopback.")
    token_file = os.environ.get("HELIX_MCP_TOKEN_FILE")
    token = Path(token_file).read_text(encoding="utf-8").strip() if token_file else os.environ.get("HELIX_MCP_TOKEN", "").strip()
    if not token:
        raise ValueError("Set HELIX_MCP_TOKEN_FILE or HELIX_MCP_TOKEN.")
    # ssl.create_default_context honors SSL_CERT_FILE for private CA certificates.
    context = ssl.create_default_context()
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token},
                                 verify=context, timeout=30, follow_redirects=False) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                status = await session.call_tool("service_status", {})
                if status.isError:
                    raise ValueError("service_status returned a tool error.")
                print(json.dumps({"server": initialized.serverInfo.model_dump(),
                                  "protocol_version": initialized.protocolVersion,
                                  "tools": [tool.name for tool in listed.tools],
                                  "status": status.structuredContent}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(probe(args.url))
        return 0
    except Exception as exc:
        # Do not print request headers, response bodies, or secret-bearing context.
        print("MCP probe failed (" + type(exc).__name__ + "). Check URL, CA trust, token and server logs.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
