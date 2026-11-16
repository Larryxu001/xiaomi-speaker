"""Talk to the host-side installer (installer/installer.py) over its Unix socket.

The socket and the shared secret are mounted into the console container by
docker-compose.yml. If they're missing, the installer isn't set up yet and
every call raises InstallerUnavailable with an explanation.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

SOCKET = Path(os.environ.get("INSTALLER_SOCKET", "/run/xiaomi-speaker-installer/installer.sock"))
SECRET_FILE = Path(os.environ.get("INSTALLER_SECRET_FILE", "/etc/xiaomi-speaker/installer.secret"))


class InstallerUnavailable(Exception):
    pass


class InstallerError(Exception):
    pass


def _client() -> httpx.AsyncClient:
    if not SOCKET.exists():
        raise InstallerUnavailable("服务器上的安装器还没有启用。")
    try:
        secret = SECRET_FILE.read_text().strip()
    except OSError:
        raise InstallerUnavailable("读不到安装器的密钥文件。")
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=str(SOCKET)),
        base_url="http://installer",
        headers={"Authorization": f"Bearer {secret}"},
        timeout=httpx.Timeout(30.0),
    )


async def _call(method: str, path: str, json: dict | None = None) -> dict:
    async with _client() as client:
        try:
            resp = await client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise InstallerUnavailable(f"连不上安装器：{exc}")
    data = resp.json()
    if resp.status_code >= 400:
        raise InstallerError(data.get("error") or f"安装器返回错误（HTTP {resp.status_code}）")
    return data


async def status() -> dict:
    return await _call("GET", "/status")


async def list_apps() -> list[dict]:
    return (await _call("GET", "/apps"))["apps"]


async def install(app_id: str, params: dict, dry_run: bool = False) -> str:
    return (await _call("POST", f"/apps/{app_id}/install", {"params": params, "dry_run": dry_run}))["job_id"]


async def uninstall(app_id: str, delete_data: bool = False) -> str:
    return (await _call("POST", f"/apps/{app_id}/uninstall", {"delete_data": delete_data}))["job_id"]


async def control(app_id: str, action: str) -> None:
    await _call("POST", f"/apps/{app_id}/{action}")


async def logs(app_id: str) -> str:
    return (await _call("GET", f"/apps/{app_id}/logs"))["logs"]


async def job(job_id: str) -> dict:
    return await _call("GET", f"/jobs/{job_id}")


async def secret(app_id: str) -> dict:
    return await _call("GET", f"/apps/{app_id}/secret")


async def load_image(file_name: str) -> str:
    return (await _call("POST", "/images/load", {"file_name": file_name}))["job_id"]
