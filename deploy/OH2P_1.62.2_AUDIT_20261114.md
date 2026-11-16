# OH2P 1.62.2 补丁固件审计报告（2026-11-14）

审计对象：`deploy/client-patch/assets/mico_all_616cd9d93_1.62.2/root-patched.squashfs`
SHA256：`7be9c15810e286063c9466a53a2323680135dffada5e9604a1a43f60690df189`（与刷入文件一致）
方法：macOS 原生 `unsquashfs`（squashfs 4.7.5）只读解包，与原版 `root.squashfs` 逐文件对比。未使用 Docker，未重刷，未扫网。

## 一、结论

1. **补丁固件本身没有问题。** 四个补丁全部已实际写入镜像，Dropbear 具备完整的开机启动条件。
2. **SSH 连不上的最可能原因不在固件里，而是设备很可能仍在从 system1（原版）启动。** 依据见第三节。
3. 不需要重新构建或重刷 system0。下一步是验证启动槽位，而不是改固件。

## 二、逐项检查结果

| 检查对象 | 结果 | 说明 |
| --- | --- | --- |
| `/etc/init.d/dropbear` | ✅ 补丁生效 | `validation failed and skip`、`enable=0` 退出判断已移除、蓝牙模式/`ssh_en`/`channel=release` 门禁全部删除、`mkdir -p /data/etc/dropbear` 已加入。文件可执行（0755）。 |
| `/etc/config/dropbear` | ✅ 正确 | `PasswordAuth 1`、`RootPasswordAuth 1`、`Port 22`、无 `Interface` 限制（监听全部地址）、`rsakeyfile` 指向 `/data/etc/dropbear/`。 |
| `/etc/rc.d/` | ✅ 正确 | `S50dropbear -> ../init.d/dropbear` 存在；`S95done` 存在（`done` 脚本负责执行 `rc.local`）。 |
| `/etc/rc.local` | ✅ 补丁生效 | 保留原始内容，新增 `[ -f /data/init.sh ] && sh /data/init.sh ... &`。 |
| `/etc/passwd` | ✅ 正确 | `root:x:0:0:root:/root:/bin/ash` |
| `/etc/shadow` | ✅ 正确 | root 字段为 `$1$openxiao$…`（md5crypt，盐 `openxiao`），`{SSH_PASSWORD}` 占位符已替换，无残留。权限 0600。 |
| `/etc/inittab` | ✅ 补丁生效 | `ttyS0` 改为 `/bin/ash --login`（串口免密）。 |
| `/etc/pam.d/common-auth` | ✅ 补丁生效 | `libmico-pam.so` 两行已注释，`pam_unix.so nullok_secure` 已启用。 |
| `/bin/ota`、`/bin/flash.sh`、`/etc/init.d/wireless` | ✅ 补丁生效 | OTA 三处入口均提前 return/exit。`wireless` 补丁带 offset 应用（1393 行，位于 `ota()` 函数内，位置正确），镜像中多出一个无害的 `wireless.orig`。 |
| `/usr/sbin/dropbear` | ✅ 存在 | ARM EABI5 armhf 动态链接，`dropbearkey` 软链存在。 |
| `/dev/console` | ✅ 存在 | `c 5,1`，`mksquashfs -p` 已补回。 |
| 镜像参数 | ✅ 可用 | xz 压缩、128K 块、单 uid。与原版差异：未写入 xz 压缩器选项（内核解压不受影响）、不可 NFS 导出（无关）。 |

四件事分开判定：
1. 补丁文件存在 —— 是。
2. 补丁已成功应用到镜像 —— 是（以上全部核实）。
3. Dropbear 在该 rootfs 中具备启动条件 —— 是（rc.d 链接 + 门禁全部移除 + 配置正确）。
4. Dropbear 在实际设备上监听 22 —— **无法从镜像判断，取决于设备是否真的从 system0 启动。**

## 三、为什么怀疑设备仍从 system1 启动

1. 设备读出的 `backups-oh2p-20261114/system1-backup.bin` 前 31,728,742 字节与 OTA 原版 `root.squashfs` **SHA256 完全相同**，其后全为 `0xFF`。即 system1 = 原版 1.62.2。
2. 原生 `boardupgrade.sh` 的 A/B 逻辑：`boot_part=boot0` 时升级写入 part 1，反之写入 part 0，升级成功后切换 `boot_part`。因此**最新版本所在的槽位就是当前激活槽位**。设备显示 1.62.2 且 system1 = 1.62.2，说明刷机前激活的是 **boot1/system1**，system0 存放的是更旧的版本。
3. `boot0-backup.bin`（ANDROID! 头）与 OTA 的 `boot.img.encrypt` 不一致（kernel 长度字段 0x3a9000 vs 0x3aa000），佐证槽位 0 是旧版本。
4. 现在刷入 system0 的补丁 rootfs 能否被启动，完全取决于 USB 下发的 `setenv boot_part boot0; saveenv` 是否真正写入 env 分区。`bulkInReply success` 只表示命令被接收，不表示 saveenv 成功。
5. 若 env 未改变，设备继续从 system1 启动：原生小爱完全正常、没有 SSH、没有 `_ssh._tcp` mDNS —— 与目前全部现象一致。

同时注意：boot0 里的内核版本比 system0 现在的 rootfs 旧。rootfs 内模块目录为 `4.9.61`，上游 open-xiaoai 同样只刷 system0 + 切 boot0 并可用，因此大概率兼容，但这是第二个需要用实机验证的点。

## 四、局域网侧的旁证

- ARP 表中 `192.168.1.7`（`5a:49:1f:…`）和 `192.168.1.10`（`d2:19:90:…`）第二位十六进制为 `a`/`2`，是**随机化私有 MAC**（手机/电脑特征）。音箱使用小米真实 OUI，这两台基本可以排除。
- 对 `192.168.1.2` 做了单点定向探测（miio UDP 54321 握手 + TCP 22），均无响应。
- 4 秒 `_miio._udp` mDNS 浏览无结果。小米音箱通常会广播该服务，暗示音箱可能不在 Mac 所在的二层网络（Mac 在 Wi-Fi `en1`，192.168.1.6）。

## 五、下一步（按成本从低到高）

### 1. 用米家 App 取得音箱 IP（零风险，不需要路由器权限）
米家 App → 音箱 → 右上角「…」→ 设备信息 / 网络信息，记录 **IP、MAC、所连 Wi-Fi 名称**。
- 若 Wi-Fi 名称与 Mac 不同（例如 2.4G 访客网络/另一台路由器），先把两者放到同一网络。
- 若在同一网段，只对该 IP 做单点检查：

```bash
nc -vz -w 3 <音箱IP> 22
```

```bash
nc -w 3 <音箱IP> 22 </dev/null | head -c 64
```

- 有 `SSH-2.0-dropbear` banner → 用 `deploy/OH2P_1.62.2_BUILD_NOTES.md` 里的兼容参数登录（密码默认 `open-xiaoai`，除非 `.env` 中改过）。
- 能 ping 通但 22 不通 → 基本确认设备仍在 system1 启动，进入第 2 步。

### 2. USB 刷机模式下只读验证（不写任何分区）
重新让音箱进入刷机模式（`./flash connect`），然后：

```bash
./macos/update mread store env normal 0x10000 /tmp/env.dump && strings /tmp/env.dump | grep -E 'boot_part|bootdelay'
```

```bash
./macos/update mread store system0 normal 0x1e93000 /tmp/system0.dump && shasum -a 256 /tmp/system0.dump
```

- `boot_part=boot0` 且 system0 dump 的 SHA256 等于 `7be9c158…` → 固件已就位、env 已切换，问题转向内核兼容性或 Dropbear 运行时（需串口 ttyS0 看日志，补丁已让串口免密）。
- `boot_part=boot1` → saveenv 未生效，重新执行 `./flash switch boot0` 后再读一次 env 确认，再重启。
- system0 dump 不匹配 → 写入未完成，按刷机说明重试 `./flash system system0 root-patched.squashfs`。

### 3. 恢复方案（供参考，先不执行）
- system0 原始内容虽无备份，但 OTA 原版 `root.squashfs`（SHA256 `95e1238d…`，已验证签名/CRC）本身就是合法的 1.62.2 rootfs，可作为 system0 的恢复输入。
- 任何时候 `./flash switch boot1` 都可回到完整原版 system1。

## 六、其他备注
- `deploy/client-patch/.env` 含小米账号明文密码，已被 `.gitignore` 排除，构建完成后建议按 BUILD_NOTES 删除。
- `deploy/flash-tool/` 下另有一份 `boot0-backup.bin`、`system1-backup.bin` 和 `oh2p-audit/` 解包目录，均未入库；它们和 `backups-oh2p-20261114/` 内容重复，可择一保留。
- 工作区有一处未提交修改：`deploy/client-patch/src/squashfs.sh`（macOS `wc -c` 兼容改动）。

## 七、实机验证结果（2026-11-14 晚，补充）

**SSH 已确认在线，前述“仍从 system1 启动”的怀疑不成立。**

- 米家 App 显示音箱 IP `192.168.3.6`，所连 Wi-Fi 为 `6F`；Mac 原先在另一网段（192.168.1.x），切到 `6F` 后为 `192.168.3.30`。
- `route get 192.168.3.6` 显示流量被 **Clash Verge 的 TUN 模式**（`utun10`，网关 `198.18.0.1`）劫持；未绑定接口时 ping 全丢、`nc` 22 端口出现由代理伪造的“成功”。
- 把探测绑定到 `en1` 后：ping 3/3 通，TCP 22 返回 `SSH-2.0-dropbear`，SSH 握手完整（banner 显示 `ROM Type:release / Ver:1.62.2`），因无密钥而正常拒绝。

因此四件事全部成立：补丁存在、补丁已应用、Dropbear 可启动、Dropbear 在实机监听 22。

### 连接方式
`~/.ssh/config` 已新增 `Host xiaoai`（`BindInterface en1` 绕过 TUN、兼容 ssh-rsa、ControlMaster 复用 8 小时）。首次用密码登录一次后，后续命令和 scp 均复用该连接。

```bash
ssh xiaoai
```

长期建议：在 Clash Verge 中把 `192.168.0.0/16` 加入 TUN 绕过（bypass）列表，或在操作音箱期间关闭 TUN 模式。

### 公钥登录限制
`/root` 与 `/etc/dropbear/authorized_keys` 都在只读 squashfs 上，固件没有 overlay，Dropbear 版本较老（不支持 ed25519、无 `-D` 选项）。要做持久公钥登录，需在 `/data/init.sh` 中 `mount --bind /data/root /root` 之类处理，留待 Agent 部署完成后再考虑。
