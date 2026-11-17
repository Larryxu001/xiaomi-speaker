# XiaoAI 控制台

管理小米音箱 Agent 的网页：改大模型/语音识别的服务商和 Key，看在线状态和日志，
建定时播报和 AI 新闻分析任务。音箱每 20 秒主动来问一次有没有新设置或该播报的
事，控制台从不主动连音箱，所以放哪台机器（本机、Mac mini、云服务器）都一样。

## 本地跑起来看看

```bash
cd console
docker compose up --build
```

打开 http://localhost:8088 ，第一次会要你设一个管理密码（只在这台控制台用，
和小米账号无关）。

## 让音箱连上它

1. 打开控制台，登录后随便进一个页面，打开浏览器开发者工具或者直接看数据库：
   首次启动会生成一个 `device_token`（一长串随机字符）。取出它：
   ```bash
   docker compose exec console sqlite3 /data/console.db \
     "select value from settings where key='device_token'"
   ```
2. 在能 SSH 到音箱的机器上执行（把地址换成控制台实际能访问到的地址）：
   ```bash
   XIAOAI_CONSOLE_URL=https://console.example.com \
   XIAOAI_DEVICE_TOKEN=<上一步取出的值> \
   deploy/push-console-poller.sh
   ```
   这一步把轮询脚本和新版 `/data/init.sh` 都装到音箱上，重启也会自动跑起来。

## 部署到云服务器

容器只监听 `127.0.0.1:8088`，需要一个反向代理做 HTTPS（音箱和浏览器都应该走
HTTPS）。最省事的是 [Caddy](https://caddyserver.com/)，自动签证书：

```caddyfile
console.example.com {
    reverse_proxy 127.0.0.1:8088
}
```

步骤：
1. 把 `console/` 目录传到服务器（`git clone` 或 `rsync` 都可以）。
2. `docker compose up -d --build`。
3. 配置好 Caddy/nginx 把 `console.example.com` 转发到 `127.0.0.1:8088`，
   确认 DNS 已经指向这台服务器，安全组放开 80/443。
4. 按上面"让音箱连上它"那一步，把音箱指向这个域名。

## 数据在哪

全部在 `data/console.db`（SQLite），搬家只需要把这一个文件复制过去，换个地址
重新指给音箱即可，不用重新设置一遍。

## 还没做的

- "让大模型记住我的习惯"目前只能通过定时任务里"关于你"那段文字体现，不会自动
  带进你和音箱的日常对话——那需要改音箱程序本身，之后再排。
- Home Assistant 等更多 MCP 工具：接入方式已经在 `agent.yaml` 里留好了位置，
  后续按需加开关即可。
