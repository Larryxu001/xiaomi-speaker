"""Connect to an external MCP server from the console and list its tools.

Uses the same transport rule as the speaker agent (a URL ending in /sse is
legacy SSE, anything else is streamable HTTP) and the same Bearer token, so
a passing test here means the speaker should be able to connect too, as
long as it can reach the address.
"""

from __future__ import annotations

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from .services import ServiceError, mcp_transport

PROBE_TIMEOUT_S = 15


def _explain(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    if isinstance(exc, TimeoutError) or "timeout" in low:
        return "连接超时。服务没有在 15 秒内响应。"
    if "401" in low or "403" in low or "unauthorized" in low or "forbidden" in low:
        return "被拒绝访问（401/403）。检查 Token 是否正确。"
    if "404" in low or "not found" in low:
        return "地址不对（404）。常见的路径是 /mcp 或 /sse，确认服务的 MCP 地址。"
    if "name or service not known" in low or "nodename nor servname" in low:
        return "找不到这个域名。"
    if "refused" in low or "connecterror" in low or "all connection attempts failed" in low:
        return "连不上这个地址。检查地址、端口，以及服务是否在运行。"
    return f"连接失败：{text[:200]}"


def _leaf(exc: BaseException) -> BaseException:
    """anyio wraps failures in exception groups; find the real cause."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


_STATUS_MESSAGES = {
    401: "被拒绝访问（401）。这个服务需要 Token，或者 Token 不对。",
    403: "被拒绝访问（403）。检查 Token 是否正确。",
    404: "地址不对（404）。常见的路径是 /mcp 或 /sse，确认服务的 MCP 地址。",
    405: "这个地址不接受 MCP 请求（405）。确认填的是 MCP 地址，而不是服务的首页。",
}


async def _preflight(url: str, headers: dict) -> None:
    """The MCP SDK hides HTTP status codes; check them first for clear errors."""
    try:
        async with httpx2.AsyncClient(timeout=httpx2.Timeout(PROBE_TIMEOUT_S)) as client:
            if mcp_transport(url) == "sse":
                async with client.stream("GET", url, headers={**headers, "Accept": "text/event-stream"}) as resp:
                    status = resp.status_code
            else:
                resp = await client.post(url, headers={**headers, "Accept": "application/json, text/event-stream"},
                                         json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                                             "protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "xiaomi-speaker-console", "version": "1"}}})
                status = resp.status_code
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise ServiceError(_explain(_leaf(exc))) from None
    if status in _STATUS_MESSAGES:
        raise ServiceError(_STATUS_MESSAGES[status])
    if status >= 500:
        raise ServiceError(f"服务端出错了（HTTP {status}）。")


async def list_remote_tools(url: str, token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    await _preflight(url, headers)
    try:
        with anyio.fail_after(PROBE_TIMEOUT_S):
            if mcp_transport(url) == "sse":
                async with sse_client(url, headers=headers, timeout=PROBE_TIMEOUT_S) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.list_tools()
            else:
                http_client = create_mcp_http_client(
                    headers=headers, timeout=httpx2.Timeout(PROBE_TIMEOUT_S))
                async with http_client:
                    async with streamable_http_client(url, http_client=http_client) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.list_tools()
    except BaseException as exc:  # noqa: BLE001 - surface every failure as a message
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise ServiceError(_explain(_leaf(exc))) from None
    return [{"name": t.name, "description": (t.description or "").strip()} for t in result.tools]
