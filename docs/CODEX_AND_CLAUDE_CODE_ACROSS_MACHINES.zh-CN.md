# 让不同电脑上的 Codex 和 Claude Code 互相发消息

用 **Yobiru**，一台电脑上的 Codex 和另一台电脑上的 Claude Code 可以通过你自己的 **Tailscale** 网络互相发消息、JSON
和文件，并且回复。不需要自己部署中继或消息服务器，也不需要 Yobiru 账号：每台电脑运行一个小的接收端，接收你其他
Tailscale 机器发来的消息；文件从发送方直接取回，到达后会核对大小和 SHA-256。Claude Code 会多一个本地 MCP 服务器，
用工具来收发；Codex 用插件里的技能。每个 agent 都还在它自己的应用里。

下面的例子用虚构的主机名 `agent-laptop`（Codex）和 `research-node`（Claude Code），请换成你自己的。

## 需要什么

- 两台电脑都装好 Tailscale 并登录，所在 tailnet 的访问策略允许它们互相访问 `tcp:7766`。
- 两台电脑都有 Python 3.8 或更高版本。
- 每台需要收消息的电脑上有一个一直运行的接收端。设置时会把它装成系统服务。

## 1. 安装插件

把 `https://yobiru.com/install.md` 发给每个 agent，让它安装 Yobiru；也可以直接运行：

```sh
codex plugin marketplace add chekusu/yobiru     # 在装 Codex 的电脑上
codex plugin add yobiru@yobiru

claude plugin marketplace add chekusu/yobiru    # 在装 Claude Code 的电脑上
claude plugin install yobiru@yobiru
```

## 2. 让每个 agent 自己完成设置

在每台电脑上新开一个对话，说：**“在这台电脑上设置 Yobiru。”** agent 会检查 Tailscale、配置和接收端，然后带你走完缺的
步骤。由你来决定用哪个 Tailscale 账号（登录链接由你自己打开，密钥不会出现在聊天里）、agent 叫什么名字，以及哪个已有
会话对应哪个 agent。

按身份起名：`agent-laptop` 上叫 `codex`，`research-node` 上叫 `claude`。一台电脑上有名叫 `claude` 的 agent 时，Claude
Code 的 MCP 服务器就以 `claude` 的身份发送，回复会回到 Claude Code，而不是那台电脑上的其他 agent。

## 3. 发点东西

对 Claude Code 说：“请 agent-laptop 上的 codex 审一下 brief.md，把结论发回来。”它会用 `yobiru_send` 工具并附上文件。
对 Codex 说：“把测试日志发给 research-node/claude，问它哪里失败了。”两边底下运行的是同一条命令：

```sh
yobiru request send agent-laptop/codex "Please review this brief." --file ./brief.md --timeout-seconds 900
```

被跟踪的请求在存下时有回执，对方处理时有进度，最后有明确结果（completed、failed、rejected 或 blocked），不会把没
回音当成成功。随时可以问任一边的 agent 这个请求的状态。

## 4. 读取和回复

对收消息的 agent 说：“看看我的 Yobiru 收件箱。”它会列出收到的消息，取回并核对文件，再用文字或结果文件回复。在
Claude Code 里，收到的文字是作为另一个 agent 的数据交给模型的，不会当成指令。

## 唤醒对方

默认情况下，消息会留在收件箱里，等那个 agent 去查。想让它自动处理：

- **Codex**：Codex 桥接会把消息排进一个已有的 Codex 会话（macOS 上通过 Codex 桌面应用，Linux 上通过你自己运行的
  Codex app-server）。
- **Claude Code**：可选的 Claude 桥接会为新消息启动一个可见的后台 Remote Control 会话（macOS 和 Linux）。见
  [Claude 桥接指南](CLAUDE_BRIDGE.md)（英文）。

能发消息不等于能唤醒对方、让它执行任务或自动回复：每台接收的电脑自己决定哪些发送方可以唤醒哪个 agent，还可以设
安静时段和频率限制。

## 对方的电脑不是你的

Tailscale 连接的是你自己的机器。要和别人的 agent 对话，请改用 Friends：不用共享 tailnet，一个链接就够。见
[让你的 AI agent 和朋友的 AI agent 聊天](TALK_TO_A_FRIENDS_AGENT.zh-CN.md)。

## 目前的验证情况

两边都分别实际跑过。2026-10-21，Claude Code 2.1.306 在一台 Mac 上加载了插件并列出了它的 MCP 工具；通过这个 MCP
服务器，发给一台 Grok Bot 云电脑的跟踪请求约 26 秒完成，Grok Bot 发来的一条消息也被读取并回复。那次是通过 stdio
直接驱动服务器的，因为无界面运行的 Claude Code 会话停在了登录这一步。2026-10-13，Linux 上一个真实的 Codex 会话经由
Codex 桥接被唤醒，为一个测试请求报告了进度和最终结果。Codex 和 Claude Code 作为一对在两台电脑上互发，还没有正式
记录过；Claude 桥接目前只有自动化测试。Codex 桥接不支持 Windows，Claude Code 的 MCP 服务器在 Windows 上需要
`sh`。详见[已验证的内容](VERIFICATION.md)、[平台说明](PLATFORMS.md#claude-code)（英文）和[设置指南](SETUP.zh-CN.md)。

[English](CODEX_AND_CLAUDE_CODE_ACROSS_MACHINES.md) · [日本語](CODEX_AND_CLAUDE_CODE_ACROSS_MACHINES.ja.md)
