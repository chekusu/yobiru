# 在新机器上配置 Yobiru

[English](SETUP.md) | **简体中文** | [日本語](SETUP.ja.md)

安装 Yobiru 插件本身不会运行任何东西：没有安装钩子。要正常工作，需要 Tailscale、配置文件、常驻接收端、每个 agent 的唤醒方式，以及（可选的）每个 agent 使用的会话。`yobiru onboard` 会检查所有这些并按当前平台给出下一步；它是只读的，在什么都没有时也能运行。对 agent 说“配置 Yobiru”时，`yobiru-setup` skill 会按同样的步骤进行。

以下 Tailscale 信息来自官方页面（2026-10-11 核对）：[Linux](https://tailscale.com/docs/install/linux)、[macOS](https://tailscale.com/docs/install/mac)、[Windows](https://tailscale.com/docs/install/windows)、[下载页](https://tailscale.com/download)，以及官方 Linux 脚本 <https://tailscale.com/install.sh>。

## 如何运行命令

插件不会在 PATH 中安装 `yobiru` 程序。下文的 `yobiru` 指用带引号的绝对路径调用插件里的启动脚本，与 README 的 `Y` 约定相同：

```sh
Y="/absolute/path/to/yobiru/scripts/yobiru"     # 已安装插件或解压后的 release 中的路径
sh "$Y" onboard
```

Windows 上：`py -3 "C:\path\to\yobiru\scripts\yobiru.py" onboard`。非默认数据目录写在命令之前：`sh "$Y" --home /workspace/yobiru-data onboard`。

## 分工

| 步骤 | Agent（在你要求配置之后） | 你 |
|---|---|---|
| 安装 Tailscale | 用宿主的 root/sudo 运行官方 Linux 脚本 | macOS/Windows 应用的安装与批准；需要管理员的操作 |
| 登录 | 在 Linux 上运行 `tailscale up` 并给你登录链接 | 自己打开链接（或应用）并选择账户和 tailnet；绝不把认证密钥贴进聊天 |
| 对端与名称 | 应用计划 | 决定谁可以发消息、agent 叫什么 |
| 接收服务 | 安装并启动打印出的服务定义 | — |
| 唤醒与会话 | 按你的选择配置适配器和绑定 | 指明每个 agent 对应的已有会话 |

Yobiru 从不修改 Tailscale ACL、shields-up 或 DNS。新安装允许通过 Tailscale 身份认证、网络策略允许接入且未命中本地拒绝规则的对端通信；通信权限不构成执行任务或唤醒的授权。`help`、`doctor`、`status`、`onboard`、`agents` 和 `update check` 都是只读的。

## 1. Tailscale

- **Linux**：用 root 或 sudo 运行 `curl -fsSL https://tailscale.com/install.sh | sh`，再运行 `sudo tailscale up`，它会打印登录链接。没有 root/sudo（请找管理员）、没有 `/dev/net/tun`（容器、部分云沙箱：userspace 网络模式不会给接收端一个可绑定的 Tailscale 地址，请换用支持 TUN 的主机），或没有 systemd 让 `tailscaled` 常驻（请用其他守护方式启动）时，`onboard` 会如实报告受阻，而不是猜测。
- **macOS**：从 Mac App Store 或下载页安装 Tailscale 应用，批准系统扩展／VPN 配置提示，从菜单栏登录。应用自带的 CLI 是 `/Applications/Tailscale.app/Contents/MacOS/Tailscale`；`tailscale` 不在 PATH 中时运行 `yobiru setup --tailscale-cmd /Applications/Tailscale.app/Contents/MacOS/Tailscale`（已有配置时加 `--replace tailscale_cmd`）。
- **Windows**：以管理员权限运行安装程序，从通知区域图标登录。CLI 不在 PATH 中时：`yobiru setup --tailscale-cmd "C:\Program Files\Tailscale\tailscale.exe"`。在 PowerShell 中这样调用：`& "C:\Program Files\Tailscale\tailscale.exe" status`。使用 `py -3 "<plugin>\scripts\yobiru.py"`；Windows 上没有 Codex 桥接。

`doctor` 会说明它看到的状态：未安装、守护进程未运行、无权限、未登录、已停止、等待设备批准（由 tailnet 管理员批准），或运行中。

## 2. 对端与 agent 名称

```sh
yobiru setup --agent NAME --self-agent NAME --default-agent NAME          # 计划
yobiru setup --agent NAME --self-agent NAME --default-agent NAME --apply
```

新配置采用 `tailnet` 模式，无需逐个添加白名单。若需限制新安装的通信范围，可加 `--allow PEER`（主机名、FQDN、`tag:NAME` 或 `user:LOGIN`）；`--send-only` 使用空白名单。已有访问策略会保留；修改身份需要 `--replace FIELD`；改动配置时会先备份。

已有安装先用 `yobiru policy show` 查看实际模式。迁移旧白名单时，先运行 `yobiru policy set --mode tailnet` 预览，已有主人授权后加 `--apply`。`--deny RULE` 替换整个拒绝列表，多条规则重复指定；两种模式都以拒绝规则优先。0.4 接收端和桥接在下一次权限检查时读取新策略，单独改策略无需重启；首次升级运行时仍需重启。见 [0.4 迁移指南](UPGRADING-0.4.md)。

## 3. 常驻接收端

```sh
yobiru install-runtime --apply
yobiru service-template systemd-user     # 或 systemd、launchd、windows（onboard 会给出合适的类型）
```

按定义头部说明的位置安装并启动。要接收消息，需关闭 shields-up，且 tailnet 策略允许选定的对端访问 `tcp:7766`。

## 4. 每个 agent 如何被唤醒、对应哪个会话

这是两件事。唤醒：Codex 桥接（`yobiru codex-bridge install --thread-id UUID ...`）或 `yobiru wake-config --agent NAME --exec PROGRAM | --webhook ...`；没有适配器时，消息留在 `inbox --pending`。会话：`yobiru session bind --agent NAME --session-id 准确ID [--display-name 别名] [--confirmed-title 标题]`，先预演再 `--apply`。名称本身绝不会选择或重命名会话；装有 Codex 桥接时，入口会话 UUID 会被自动报告，冲突的绑定会被拒绝。

Codex 入口使用 `workspace-write` 时，还需配置[最小沙箱权限](CODEX_SANDBOX.md)：用实际绝对路径开放 inbox 和 handled 的写入；仅带附件回复需要 outbox 写权限。先在可信安装环境准备这些目录，保持策略、运行代码和队列只读，不能把整个数据目录设为可写。只有已授权的回复／附件下载需要网络，网络权限不会覆盖 `allow_replies=false`。

## 5. 验证

```sh
yobiru doctor
yobiru agents --online
yobiru send PEER/AGENT "hello"
```

`agents` 显示每台机器的接收端状态、能否向它发送、Yobiru 版本，以及每个 agent 的会话名称（带来源）和准确 ID。要测试唤醒，从唤醒策略允许 `now` 的对端用 `--wake now` 发送，然后在接收端用 `show ID` 查看 `wake_delivery`。

## 之后的更新

按原来的安装方式更新插件：Codex／Cursor／Grok Bot 的插件页或市场、新的官方 release 压缩包（先用 `SHA256SUMS` 校验，再解压到新目录，绝不解压到数据目录上），或检出官方标签。之后改用新包里的 `scripts/yobiru`（`yobiru version` 显示新版本）；数据目录不受影响。

见 `yobiru-upgrade` skill 和 `yobiru update --help`：插件更新不会改变常驻运行时（运行 `install-runtime --upgrade --apply`，再重启接收端）；本机运行已发布的版本后，`yobiru update notify` 会通知你的其他已知安装。

已有安装保留旧白名单，策略迁移和软件升级是两件事。`update maintain --apply` 记录更新检查，且仅重试此前排队的升级通知。需要每 15 分钟及启动后运行时，用 `update reminder-template systemd-user --retry-notices`（或 `launchd`／`cron`）打印并按说明安装可选定时任务；插件路径变化后重新生成。它不安装软件、不改策略。见 [0.4 迁移指南](UPGRADING-0.4.md)。
