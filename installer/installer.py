#!/usr/bin/env python3
"""Host-side app installer for the speaker console (stdlib only).

Runs as root under systemd on the server. It listens only on a Unix socket
that is mounted into the console container, so nothing on the network can
reach it, and it accepts a small fixed set of actions on apps from the
catalog in ../apps/<id>/:

    GET  /status                 docker / memory / disk overview
    GET  /apps                   catalog entries with install state
    POST /apps/<id>/install      {"params": {...}, "dry_run": bool}
    POST /apps/<id>/uninstall    {"delete_data": bool}
    POST /apps/<id>/start | /stop
    GET  /apps/<id>/logs
    GET  /jobs/<job_id>          progress of a running install/uninstall

Every request must carry "Authorization: Bearer <secret>" where the secret is
read from SECRET_FILE (also mounted read-only into the console container).
See docs/superpowers/specs/2026-11-14-app-store-design.md.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import shutil
import socketserver
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Path(os.environ.get("INSTALLER_CATALOG", ROOT / "apps"))
APPS_HOME = Path(os.environ.get("INSTALLER_APPS_HOME", "/opt/xiaomi-speaker-apps"))
RUN_DIR = Path(os.environ.get("INSTALLER_RUN_DIR", "/run/xiaomi-speaker-installer"))
SOCKET = RUN_DIR / "installer.sock"
SECRET_FILE = Path(os.environ.get("INSTALLER_SECRET_FILE", "/etc/xiaomi-speaker/installer.secret"))
# Set INSTALLER_APP_DOMAIN to your own domain to give each app its own
# subdomain; empty means apps are served under the console's own address.
APP_DOMAIN = os.environ.get("INSTALLER_APP_DOMAIN", "")
NGINX_SITES = Path(os.environ.get("INSTALLER_NGINX_DIR", "/etc/nginx"))
# Apps are served under the console's own address by default
# (https://<console>/apps/<id>/), so installing one needs no DNS or new cert.
# The console's nginx server block includes PATH_DIR/*.conf.
# The address the console itself is reached at; apps are served under it.
CONSOLE_HOST = os.environ.get("INSTALLER_CONSOLE_HOST") or (f"xiaomi.{APP_DOMAIN}" if APP_DOMAIN else "")
PATH_DIR = NGINX_SITES / "xiaomi-speaker-apps"
# Apps marked internal (only the console uses them) accept requests from the
# server itself only; its calls to its own domain arrive from this address.
SERVER_IPS = os.environ.get("INSTALLER_SERVER_IPS", "124.222.75.105 127.0.0.1").split()
MEM_RESERVE_MB = 400  # always leave this much free for the system and other sites
# Docker Hub is often unreachable from mainland China: try these mirrors
# first, then ask the owner to upload the image (see /images/load).
MIRRORS = [m for m in os.environ.get("INSTALLER_MIRRORS", "docker.m.daocloud.io").split(",") if m]
UPLOAD_DIR = Path(os.environ.get("INSTALLER_UPLOAD_DIR", ROOT / "console" / "data" / "uploads"))

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
_jobs: dict[str, dict] = {}
_lock = threading.Lock()  # one install/uninstall at a time


class InstallError(Exception):
    pass


# --------------------------------------------------------------- helpers ----


def run(cmd: list[str], job: dict | None = None, check: bool = True, timeout: int = 900) -> str:
    if job is not None:
        job["log"].append("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout + proc.stderr).strip()
    if job is not None and out:
        job["log"].append(out[-4000:])
    if check and proc.returncode != 0:
        raise InstallError(f"命令失败：{' '.join(cmd[:3])}…（退出码 {proc.returncode}）")
    return out


def mem_available_mb() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def disk_free_gb(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / 1024 ** 3


def parse_app_yaml(path: Path) -> dict:
    """Tiny parser for the catalog's flat app.yaml (avoids a PyYAML dependency).

    Supports `key: value`, one level of `section:` nesting, and `- item` lists
    of `key: value` maps, which is all the catalog format uses.
    """
    data: dict = {}
    section = None
    current_item = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent == 0:
            key, _, value = line.partition(":")
            value = value.strip()
            if value:
                data[key.strip()] = _scalar(value)
                section = None
            else:
                section = key.strip()
                data[section] = None
            current_item = None
        elif section and line.startswith("- "):
            if not isinstance(data[section], list):
                data[section] = []
            current_item = {}
            data[section].append(current_item)
            k, _, v = line[2:].partition(":")
            current_item[k.strip()] = _scalar(v.strip())
        elif section and current_item is not None and indent >= 4:
            k, _, v = line.partition(":")
            current_item[k.strip()] = _scalar(v.strip())
        elif section:
            if not isinstance(data[section], dict):
                data[section] = {}
            k, _, v = line.partition(":")
            data[section][k.strip()] = _scalar(v.strip())
    return data


def _scalar(value: str):
    if value.startswith(("'", '"')) and value.endswith(value[0]):
        return value[1:-1]
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def catalog() -> dict[str, dict]:
    apps = {}
    if not CATALOG.is_dir():
        return apps
    for d in sorted(CATALOG.iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")) or not _ID.match(d.name):
            continue
        if not (d / "app.yaml").is_file() or not (d / "docker-compose.yml").is_file():
            continue
        meta = parse_app_yaml(d / "app.yaml")
        meta["id"] = d.name
        apps[d.name] = meta
    return apps


def app_dir(app_id: str) -> Path:
    return APPS_HOME / app_id


def project(app_id: str) -> str:
    return f"xs-{app_id}"


def installed_state(app_id: str) -> dict:
    state_file = app_dir(app_id) / "state.json"
    if not state_file.is_file():
        return {"installed": False}
    state = json.loads(state_file.read_text())
    if shutil.which("docker"):
        out = run(["docker", "compose", "-p", project(app_id), "ps", "--format", "{{.State}}"], check=False)
        states = [s for s in out.splitlines() if s]
        state["running"] = bool(states) and all(s == "running" for s in states)
    return state


# ----------------------------------------------------------------- images ----


def compose_images(app_id: str) -> list[str]:
    text = (CATALOG / app_id / "docker-compose.yml").read_text(encoding="utf-8")
    return re.findall(r"^\s*image:\s*([^\s#]+)", text, re.M)


def image_present(image: str) -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


def hub_link(image: str) -> str:
    repo, _, tag = image.partition(":")
    if "/" not in repo:
        return f"https://hub.docker.com/_/{repo}?tab=tags&name={tag}"
    return f"https://hub.docker.com/r/{repo}/tags?name={tag}"


def image_help(image: str) -> dict:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", image)
    return {
        "image": image,
        "link": hub_link(image),
        "save_command": (f"docker pull --platform linux/amd64 {image} && "
                         f"docker save {image} | gzip > {safe}.tar.gz"),
        "file_name": f"{safe}.tar.gz",
    }


def ensure_images(app_id: str, job: dict) -> list[dict]:
    """Make every image local: already present, or pulled via a mirror and
    re-tagged. Returns help entries for the ones that could not be fetched."""
    missing = []
    for image in compose_images(app_id):
        if image_present(image):
            job["log"].append(f"镜像已在服务器上：{image}")
            continue
        fetched = False
        for mirror in MIRRORS:
            mirrored = f"{mirror}/{image if '/' in image.split(':')[0] else 'library/' + image}"
            job["log"].append(f"从镜像源拉取：{mirrored}")
            proc = subprocess.run(["docker", "pull", mirrored], capture_output=True, text=True, timeout=1800)
            if proc.returncode == 0:
                run(["docker", "tag", mirrored, image], job)
                fetched = True
                break
            job["log"].append(f"镜像源拉取失败：{(proc.stderr or proc.stdout).strip()[-200:]}")
        if not fetched:
            missing.append(image_help(image))
    return missing


def load_uploaded(file_name: str, job: dict) -> dict:
    path = (UPLOAD_DIR / file_name).resolve()
    if path.parent != UPLOAD_DIR.resolve() or not path.is_file():
        raise InstallError("找不到上传的镜像文件。")
    try:
        out = run(["docker", "load", "-i", str(path)], job, timeout=1800)
    finally:
        path.unlink(missing_ok=True)
    loaded = re.findall(r"Loaded image: (\S+)", out)
    if not loaded:
        raise InstallError("这个文件里没有找到镜像。请用页面上给出的 docker save 命令导出。")
    return {"loaded": loaded}


# ---------------------------------------------------------------- actions ----


def _path_conf(app_id: str, meta: dict, mcp_token: str | None) -> str:
    port = int(meta["web_port"])
    prefix = f"/apps/{app_id}/"
    upstream = f"http://127.0.0.1:{port}" + ("" if meta.get("keep_prefix") else "/")
    access = ""
    if meta.get("internal"):
        access = "".join(f"        allow {ip};\n" for ip in SERVER_IPS) + "        deny all;\n"
    mcp_block = ""
    if meta.get("mcp_path") and mcp_token:
        mp = meta["mcp_path"]
        mcp_block = f"""
    # MCP endpoint for the speaker: Bearer token required.
    location = /apps/{app_id}{mp} {{
        if ($http_authorization != "Bearer {mcp_token}") {{ return 403; }}
        proxy_pass http://127.0.0.1:{port}{mp};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }}
"""
    return f"""# Managed by xiaomi-speaker installer for app "{app_id}". Do not edit by hand.
{mcp_block}
    location ^~ {prefix} {{
{access}        proxy_pass {upstream};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Prefix {prefix.rstrip('/')};
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
        client_max_body_size 512m;
    }}
"""


def _console_includes_apps() -> bool:
    for f in (NGINX_SITES / "sites-enabled").glob("*"):
        try:
            text = f.read_text()
        except OSError:
            continue
        if CONSOLE_HOST in text and str(PATH_DIR) in text:
            return True
    return False


def _nginx_conf(app_id: str, meta: dict, mcp_token: str | None) -> str:
    host = f"{app_id}.{APP_DOMAIN}"
    port = int(meta["web_port"])
    mcp_block = ""
    if meta.get("mcp_path") and mcp_token:
        mcp_block = f"""
    # MCP endpoint for the speaker: Bearer token required.
    location {meta['mcp_path']} {{
        if ($http_authorization != "Bearer {mcp_token}") {{ return 403; }}
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }}
"""
    return f"""# Managed by xiaomi-speaker installer for app "{app_id}". Do not edit by hand.
server {{
    listen 80;
    server_name {host};
{mcp_block}
    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        client_max_body_size 512m;
    }}
}}
"""


def install(app_id: str, params: dict, dry_run: bool, job: dict) -> dict:
    apps = catalog()
    if app_id not in apps:
        raise InstallError("商店里没有这个应用。")
    meta = apps[app_id]
    if installed_state(app_id).get("installed"):
        raise InstallError("这个应用已经装过了。")

    limit_mb = int(meta.get("memory_mb", 256))
    need_mb = int(meta.get("idle_mb", limit_mb))
    free_mb = mem_available_mb()
    job["log"].append(f"内存：平时约占 {need_mb}MB（上限 {limit_mb}MB），当前可用 {free_mb}MB（保留 {MEM_RESERVE_MB}MB 给系统）")
    if free_mb - need_mb < MEM_RESERVE_MB:
        raise InstallError(f"内存不够：平时约占 {need_mb}MB，可用 {free_mb}MB，还要给系统和其他网站留 {MEM_RESERVE_MB}MB。可以先停掉不常用的应用。")
    need_gb = float(meta.get("disk_gb", 1))
    free_gb = disk_free_gb(APPS_HOME)
    job["log"].append(f"硬盘：需要约 {need_gb}GB，当前可用 {free_gb:.1f}GB")
    if free_gb - need_gb < 2:
        raise InstallError(f"硬盘不够：需要约 {need_gb}GB，可用 {free_gb:.1f}GB。")

    # Apps that need to know their own public address (n8n, SearXNG) read
    # these instead of carrying one household's domain in their compose file.
    app_host = f"{app_id}.{APP_DOMAIN}" if APP_DOMAIN and meta.get("route") == "domain" else CONSOLE_HOST
    app_url = f"https://{app_host}" + ("" if meta.get("route") == "domain" else f"/apps/{app_id}")
    env_lines = [f"APP_DATA={app_dir(app_id) / 'data'}", f"APP_MEMORY={limit_mb}m",
                 f"APP_HOST={app_host}", f"APP_URL={app_url}"]
    for p in meta.get("params") or []:
        key = p["key"]
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,40}", key):
            raise InstallError(f"应用参数名 {key} 不合法。")
        value = str(params.get(key, p.get("default", "")))
        if p.get("required") and not value:
            raise InstallError(f"请填写「{p.get('label', key)}」。")
        if p.get("generate") and not value:
            value = secrets.token_urlsafe(18)
        if "\n" in value:
            raise InstallError(f"「{p.get('label', key)}」不能包含换行。")
        env_lines.append(f"{key}={value}")

    mcp_token = secrets.token_urlsafe(32) if meta.get("mcp_path") else None
    route = meta.get("route", "path") if meta.get("web_port") else None
    if route and not CONSOLE_HOST:
        raise InstallError("还不知道控制台的网址，没法给应用配置入口。"
                           "请在安装器的环境变量里设置 INSTALLER_CONSOLE_HOST（例如 console.example.com）。")
    if route == "path":
        nginx_conf = _path_conf(app_id, meta, mcp_token)
        host = CONSOLE_HOST
        base_url = f"https://{CONSOLE_HOST}/apps/{app_id}"
        if not dry_run and not _console_includes_apps():
            raise InstallError(f"控制台的 nginx 配置里还没有引入 {PATH_DIR}/*.conf，请先运行 deploy/setup-installer.sh。")
    elif route == "subdomain":
        nginx_conf = _nginx_conf(app_id, meta, mcp_token)
        host = f"{app_id}.{APP_DOMAIN}"
        base_url = f"https://{host}"
    else:
        nginx_conf, host, base_url = None, None, None

    if dry_run:
        job["log"].append("—— 演练模式：只生成配置、做检查，不启动任何容器 ——")
        job["log"].append(".env:\n" + "\n".join(l.split("=")[0] + "=…" for l in env_lines))
        if nginx_conf:
            job["log"].append(f"nginx（{host}）:\n" + nginx_conf.replace(mcp_token or "§", "<token>"))
        if shutil.which("docker"):
            # Validate with exactly the variables a real install would use.
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as tmp:
                tmp.write("\n".join(env_lines) + "\n")
            try:
                run(["docker", "compose", "-f", str(CATALOG / app_id / "docker-compose.yml"),
                     "--env-file", tmp.name, "config", "-q"], job)
                job["log"].append("compose 配置校验通过")
            finally:
                os.unlink(tmp.name)
        else:
            job["log"].append("（本机没有 docker，跳过 compose 校验）")
        return {"dry_run": True, "host": host}

    missing = ensure_images(app_id, job)
    if missing:
        job["missing_images"] = missing
        names = "、".join(m["image"] for m in missing)
        raise InstallError(f"服务器下载不到这些镜像：{names}。请按页面上的链接自行下载后上传。")

    d = app_dir(app_id)
    (d / "data").mkdir(parents=True, exist_ok=True)
    shutil.copy(CATALOG / app_id / "docker-compose.yml", d / "docker-compose.yml")
    env_file = d / ".env"
    env_file.write_text("\n".join(env_lines) + "\n")
    env_file.chmod(0o600)
    try:
        run(["docker", "compose", "-p", project(app_id), "-f", str(d / "docker-compose.yml"),
             "--env-file", str(env_file), "up", "-d"], job)
        if route == "path":
            PATH_DIR.mkdir(exist_ok=True)
            (PATH_DIR / f"{app_id}.conf").write_text(nginx_conf)
            run(["nginx", "-t"], job)
            run(["systemctl", "reload", "nginx"], job)
        elif route == "subdomain":
            conf = NGINX_SITES / "sites-available" / f"xs-{app_id}"
            conf.write_text(nginx_conf)
            link = NGINX_SITES / "sites-enabled" / f"xs-{app_id}"
            if not link.exists():
                link.symlink_to(conf)
            run(["nginx", "-t"], job)
            run(["systemctl", "reload", "nginx"], job)
    except Exception:
        job["log"].append("安装失败，正在回滚…")
        _remove(app_id, delete_data=False, job=job)
        raise

    https_ready = route != "subdomain"
    if route == "subdomain":
        # A missing certificate shouldn't undo a working install: the app runs,
        # and its own address starts working once DNS and the cert are in place.
        try:
            run(["certbot", "--nginx", "-d", host, "--non-interactive", "--redirect"], job, timeout=300)
            https_ready = True
        except Exception:
            job["log"].append(f"应用已装好并在运行，但网址 {host} 的证书没签下来（通常是还没加 *.{APP_DOMAIN} 的 DNS 泛解析）。加好后在应用卡片上点「启用网址」。")

    state = {"installed": True, "app_id": app_id, "host": host, "installed_at": int(time.time()),
             "url": (base_url + "/") if base_url and https_ready else None, "route": route,
             "mcp_url": f"{base_url}{meta['mcp_path']}" if meta.get("mcp_path") and base_url else None,
             "mcp_token": mcp_token}
    (d / "state.json").write_text(json.dumps(state))
    (d / "state.json").chmod(0o600)
    return state


def _remove(app_id: str, delete_data: bool, job: dict) -> None:
    d = app_dir(app_id)
    if (d / "docker-compose.yml").is_file():
        run(["docker", "compose", "-p", project(app_id), "-f", str(d / "docker-compose.yml"), "down"],
            job, check=False)
    for p in (NGINX_SITES / "sites-enabled" / f"xs-{app_id}", NGINX_SITES / "sites-available" / f"xs-{app_id}",
              PATH_DIR / f"{app_id}.conf"):
        if p.exists() or p.is_symlink():
            p.unlink()
    if shutil.which("nginx"):
        run(["nginx", "-t"], job, check=False)
        run(["systemctl", "reload", "nginx"], job, check=False)
    (d / "state.json").unlink(missing_ok=True)
    if delete_data and d.is_dir():
        shutil.rmtree(d)
        job["log"].append(f"已删除数据目录 {d}")


def uninstall(app_id: str, delete_data: bool, job: dict) -> dict:
    if not installed_state(app_id).get("installed"):
        raise InstallError("这个应用没有安装。")
    _remove(app_id, delete_data, job)
    return {"uninstalled": True}


def start_job(kind: str, fn, *args) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "kind": kind, "status": "running", "log": [], "result": None, "error": None}
    _jobs[job_id] = job

    def worker():
        if not _lock.acquire(blocking=False):
            job.update(status="failed", error="已有一个安装或卸载在进行，请稍后再试。")
            return
        try:
            job["result"] = fn(*args, job)
            job["status"] = "done"
        except InstallError as exc:
            job.update(status="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            job.update(status="failed", error=f"意外错误：{exc}")
        finally:
            _lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return job_id


# ------------------------------------------------------------------ http ----


class Handler(BaseHTTPRequestHandler):
    server_version = "xiaomi-speaker-installer/1"

    def log_message(self, fmt, *args):  # keep journald quiet but useful
        print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}", flush=True)

    def _auth(self) -> bool:
        expected = SECRET_FILE.read_text().strip() if SECRET_FILE.is_file() else ""
        supplied = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return bool(expected) and hmac.compare_digest(supplied, expected)

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(min(length, 65536)) or b"{}")

    def do_GET(self):  # noqa: N802
        if not self._auth():
            return self._send(403, {"error": "forbidden"})
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if parts == ["status"]:
            return self._send(200, {
                "docker": bool(shutil.which("docker")), "nginx": bool(shutil.which("nginx")),
                "certbot": bool(shutil.which("certbot")), "memory_available_mb": mem_available_mb(),
                "disk_free_gb": round(disk_free_gb(APPS_HOME), 1), "app_domain": APP_DOMAIN,
                "memory_reserve_mb": MEM_RESERVE_MB})
        if parts == ["apps"]:
            out = []
            for app_id, meta in catalog().items():
                state = installed_state(app_id)
                state.pop("mcp_token", None)
                images = [{**image_help(i), "present": image_present(i)} for i in compose_images(app_id)]
                out.append({**{k: v for k, v in meta.items()}, "state": state, "images": images})
            return self._send(200, {"apps": out})
        if len(parts) == 3 and parts[0] == "apps" and parts[2] == "logs" and _ID.match(parts[1]):
            d = app_dir(parts[1])
            if not (d / "docker-compose.yml").is_file():
                return self._send(404, {"error": "这个应用没有安装。"})
            logs = run(["docker", "compose", "-p", project(parts[1]), "-f", str(d / "docker-compose.yml"),
                        "logs", "--tail", "200", "--no-color"], check=False)
            return self._send(200, {"logs": logs[-20000:]})
        if len(parts) == 2 and parts[0] == "jobs":
            job = _jobs.get(parts[1])
            return self._send(200, job) if job else self._send(404, {"error": "没有这个任务。"})
        if len(parts) == 3 and parts[0] == "apps" and parts[2] == "secret" and _ID.match(parts[1]):
            # The console needs the MCP token to register the app with the speaker.
            state = installed_state(parts[1])
            return self._send(200, {"mcp_url": state.get("mcp_url"), "mcp_token": state.get("mcp_token")})
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._auth():
            return self._send(403, {"error": "forbidden"})
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if parts == ["images", "load"]:
            try:
                body = self._body()
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON"})
            name = str(body.get("file_name", ""))
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", name):
                return self._send(400, {"error": "文件名不合法。"})
            job_id = start_job("load", lambda job: load_uploaded(name, job))
            return self._send(202, {"job_id": job_id})
        if len(parts) != 3 or parts[0] != "apps" or not _ID.match(parts[1]):
            return self._send(404, {"error": "not found"})
        app_id, action = parts[1], parts[2]
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})
        if action == "install":
            job_id = start_job("install", install, app_id, body.get("params") or {}, bool(body.get("dry_run")))
            return self._send(202, {"job_id": job_id})
        if action == "uninstall":
            job_id = start_job("uninstall", uninstall, app_id, bool(body.get("delete_data")))
            return self._send(202, {"job_id": job_id})
        if action in ("start", "stop"):
            d = app_dir(app_id)
            if not (d / "docker-compose.yml").is_file():
                return self._send(404, {"error": "这个应用没有安装。"})
            run(["docker", "compose", "-p", project(app_id), "-f", str(d / "docker-compose.yml"),
                 "start" if action == "start" else "stop"], check=False)
            return self._send(200, {"ok": True})
        if action == "certify":
            state = installed_state(app_id)
            if not state.get("installed") or state.get("route") != "subdomain":
                return self._send(400, {"error": "这个应用不需要单独的网址。"})
            try:
                run(["certbot", "--nginx", "-d", state["host"], "--non-interactive", "--redirect"], timeout=300)
            except Exception:
                return self._send(400, {"error": f"证书还是没签下来。请确认 *.{APP_DOMAIN} 已经解析到服务器，稍等几分钟再试。"})
            state["url"] = f"https://{state['host']}/"
            d = app_dir(app_id)
            (d / "state.json").write_text(json.dumps(state))
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("local", 0)  # BaseHTTPRequestHandler expects a (host, port)


def main() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if SOCKET.exists():
        SOCKET.unlink()
    server = UnixHTTPServer(str(SOCKET), Handler)
    os.chmod(SOCKET, 0o660)
    print(f"installer listening on {SOCKET}; catalog {CATALOG}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
