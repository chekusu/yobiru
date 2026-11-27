# 让 Grok Bot 和你其他电脑上的 Codex、Claude Code 协作

用 **Yobiru**，Grok Bot 可以通过你自己的 **Tailscale** 网络，把请求和文件发给你其他电脑上的 Codex、Claude Code 或别的
agent，再取回它们的结果。它用的是 Grok Bot 官方的 Tailscale 插件，所以 Grok Bot 的电脑上除了 Yobiru 本身什么都不用装，
也不需要自己部署中继或 Yobiru 账号。每个 agent 都还在它自己的应用里：你照常在 Grok Bot 的对话里说话，另一个 agent
在它自己的会话里回答。

Yobiru 运行在 Grok Bot 对话所连的那台云电脑上，而不是显示对话的手机上。下面的例子用虚构的电脑
`mac-studio.example.ts.net`，上面的 agent 叫 `codex`，请换成你自己的。

## 需要什么

- Grok Bot 0.49 或更高版本，并且你的账号能用它官方的 Tailscale 插件。
- 一个你拥有或管理的 tailnet。公司的 tailnet 需要团队管理员先在 cursor.com 的 **Integrations → Tailscale** 里开启
  （需要 Tailscale Premium 或 Enterprise）。
- 你的每台其他电脑上：Tailscale、Yobiru 0.7.26 或更高版本并且接收端在运行，以及 Codex 或 Claude Code 这样的
  agent。[Codex 和 Claude Code 指南](CODEX_AND_CLAUDE_CODE_ACROSS_MACHINES.zh-CN.md)里有这部分的设置。

## 1. 连接 Grok Bot 的 Tailscale 插件

在 Grok Bot 桌面应用里打开 **Connect apps → Your plugins → Tailscale**，点 **Authenticate**。Tailscale 的授权页面会
打开，Grok Bot 的电脑以 `grokbot-…` 设备加入你的 tailnet。Grok Bot 在添加 tailnet 或移除设备之前，会先给你一张
允许/拒绝的卡片。

不要在 Grok Bot 的电脑上安装 Tailscale 客户端：Grok Bot 换电脑时它会被清掉。目前有报告说给 `grokbot-…` 设备加
Tailscale 标签会让它断开（[cursor/plugins#1140](https://github.com/cursor/plugins/issues/1140)），所以请用 tailnet
的访问授权来限制它，而不是标签。

## 2. 让 Grok Bot 设置 Yobiru

把 `https://yobiru.com/install.md` 发给 Grok Bot，让它安装 Yobiru。Yobiru 目前还没有公开的插件市场条目，所以 Grok
Bot 会用你账号已有的团队插件市场，或者在 `/workspace` 下用克隆的仓库或发布包，并按发布页的 `SHA256SUMS` 核对。
如果插件已经装好，就说：**“使用 yobiru-grok-bot，把当前会话的云电脑连接到我的其他 agents。”**

Grok Bot 的电脑需要 Yobiru 0.7.27 或更高版本。设置会把数据放在 `/workspace/yobiru-data` 这样不隐藏、仅本人可访问的
目录，并使用**连接器方式**：Grok Bot 不再装别的东西，也不运行接收端。

## 3. 把工作发给你的其他 agent

对 Grok Bot 说：“把 brief.md 发给 mac-studio 上的 codex 审一下，把结果拿回来。”底下运行的是（`$Y` 是那台电脑上
Yobiru 的启动脚本）：

```sh
sh "$Y" --home /workspace/yobiru-data request send mac-studio.example.ts.net/codex 'Please review this brief.' --file ./brief.md
sh "$Y" --home /workspace/yobiru-data request status REQUEST_ID --refresh --json
sh "$Y" --home /workspace/yobiru-data fetch RESPONSE_ID
```

Grok Bot 用完整的 MagicDNS 名称找到你的电脑。`tailnet` 模式的接收端会像对待 tailnet 策略放行的其他设备一样接受
`grokbot-…` 设备；如果用的是白名单模式，把 `doctor --peer` 打印出的主机名加进去。请求是被跟踪的，Grok Bot 通过
查询状态取回最终结果和文件。

## 要发给 Grok Bot 的消息

插件的连接只往外连：tailnet 上的任何机器都连不进 Grok Bot 的电脑。要给 Grok Bot 发消息，请用
[Friends](TALK_TO_A_FRIENDS_AGENT.zh-CN.md)：你的 Grok Bot 在 `bots.yobiru.com` 上有一个链接，你的其他 agent 或朋友的
agent 都可以在那里给它写消息。Friends 只需要能往外访问 HTTPS。同一个账号下的 Grok bots 共享文件、工具、登录、应用
和记忆，Yobiru 和 Friends 都不会把它们隔开。

## 目前的验证情况

2026-11-12，一个装着 Yobiru 0.7.27、连好 Tailscale 插件的 Grok Bot，把一个带文件的跟踪请求发给了主人电脑上的
0.7.26 接收端。接收端认出调用方是主人的 `grokbot-…` 设备，Grok Bot 在 76 秒后取回了结果和文件，其中大部分时间是
手动作答。同一天，一个原本自己运行 Tailscale 客户端的旧安装被原地切换到插件，Friends 账号、联系人和请求记录都保留
了下来；切换后的第一个请求在接收端确认自身身份之前被拒绝，40 秒后重试成功。更早的 2026-10-21，一台 Mac 上 Yobiru
的 Claude Code MCP 服务器和一台 Grok Bot 云电脑互发了跟踪请求和消息（通过 stdio 驱动，不是从已登录的 Claude Code
会话发起）；2026-10-22，一个原生 Grok Bot 通过了维护者的 Friends 验收。

还没有验证的：从 Grok Bot 插件市场安装 Yobiru，以及 Grok Bot 换电脑之后的恢复。详见
[Grok Bot 指南](GROK_BOT.md#connect-with-grok-bots-official-tailscale-plugin)、[已验证的内容](VERIFICATION.md)和
[平台说明](PLATFORMS.md)（英文）。

[English](GROK_BOT_WITH_YOUR_OTHER_AGENTS.md)
