# 应用目录

控制台「应用」页和语音指令「小爱同学，安装……」能装的软件，都来自这里。
每个应用是一个文件夹，名字就是应用 ID，也是它的网址前缀
（例如 `navidrome/` → `https://navidrome.speaker.xulangli.com.cn`）。

一个应用包含两个文件：

- `app.yaml`：名称、简介、资源占用、网页端口、MCP 路径、安装参数。
- `docker-compose.yml`：怎么运行它。只绑定 `127.0.0.1`，写死镜像版本。

格式说明和全部字段见 [`_template/`](_template/)。以 `_` 开头的文件夹不会出现在商店里。

安装由服务器上的安装器 `installer/installer.py` 完成：先核对内存和硬盘，再启动容器、
写 nginx 配置并签证书；带 MCP 的应用会自动接到音箱。整体设计见
`docs/superpowers/specs/2026-11-14-app-store-design.md`。

目前目录里还没有任何应用，等确定要装什么再加。
