"""Ready-made MCP services that work from mainland China (verified 2026-11-15).
Each needs only the user's own key; connecting fills in the address."""

from __future__ import annotations

from urllib.parse import quote

MARKET = [
    {
        "id": "bailian", "name": "阿里云百炼 MCP 广场", "icon": "🧩",
        "desc": "百炼上架的几十个现成 MCP（快递、股票、新闻、翻译等），在百炼开通后填服务 ID 即可接入。",
        "say": ["看开通的服务"],
        "key_label": "百炼 API Key",
        "key_help": "服务 ID：百炼「MCP 广场」点开一个服务 →「立即开通」→ 详情页「接入方式」里的地址，/mcps/ 后面那段（不是 API Key 的编号）。API Key：百炼「API Key」页，sk- 开头的那串。",
        "key_link": "https://bailian.console.aliyun.com/?tab=mcp#/mcp-market",
        "url": "https://dashscope.aliyuncs.com/api/v1/mcps/{server}/mcp", "token": "{key}",
        "server_label": "服务 ID",
    },
]


def find(entry_id: str) -> dict | None:
    return next((m for m in MARKET if m["id"] == entry_id), None)


def build(entry: dict, key: str, server: str = "") -> tuple[str, str, str]:
    """(name, url, token) for save_mcp_server."""
    key, server = key.strip(), server.strip()
    if "/mcps/" in server:  # a pasted address: keep just the service ID
        server = server.split("/mcps/", 1)[1].split("/", 1)[0]
    url = entry["url"].replace("{key}", quote(key, safe="")).replace("{server}", quote(server, safe="-_"))
    token = entry["token"].replace("{key}", key)
    name = entry["id"] if entry["id"] != "bailian" else f"bailian-{server}".lower()[:40]
    return name, url, token
