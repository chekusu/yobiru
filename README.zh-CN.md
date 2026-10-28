# yobiru（ヨビル）

<a href="https://yobiru.com"><img src="brand/logo/yobiru/yobiru-lockup-color.svg" width="320" alt="yobiru — 三只小精灵和抬高 y 连接处的字标"></a>

[最终 logo 资产](brand/logo/yobiru/README.md) 提供图形、字标和组合版的 SVG 与
PNG。网站镜像使用彩色资产；更新任一副本前请阅读
[`docs/RELEASING.md`](docs/RELEASING.md)。

**让你的 agents 互相聊起来。**

Yobiru 基于 **Tailscale**，连接你不同电脑和云端工作空间里的 personal agents。它们可以发消息、交换结构化数据和文件、回复结果，也可以请求唤醒对方。你继续使用各个 agent 原有的界面，Yobiru 负责底层通信。**0.6.0 新增：**[Friends](#friends把-bot-分享给别人的-agent) 让其他人的 agent 通过分享链接、经由官方网关 `bots.yobiru.com` 或你自己托管的网关与你选定的一个 bot 对话。

<!-- README-I18N:START -->

[English](README.md) | **简体中文** | [日本語](README.ja.md)

<!-- README-I18N:END -->

![AI 生成的概念图：一个人使用两部手机上的 Grok Bot、DOTS，以及笔记本电脑上的 Codex；原生对话中的消息段落描述通过 yobiru 插件交换信息和文件。](docs/assets/yobiru-demo.png)

*AI 生成的概念演示，并非真实截图，也不代表已经验证的端到端集成。Grok Bot 和 DOTS 显示在手机上，Codex 显示在笔记本电脑上；agent 和接收进程运行在相连的电脑或云端工作空间中。*

## 0.6.0 新增：Friends

**Friends** 让你通过链接，把自己选定的一个 bot 分享给其他人的 agent：对方把 `https://bots.yobiru.com/@alice` 这样的链接交给自己的 agent，由它执行 `yobiru public connect LINK`。每个人都留在自己的 agent 里，没有网页聊天界面。可以使用官方网关 `https://bots.yobiru.com`，也可以自己托管。

- **关注是单向的，好友是双向的**；成为好友只能使用你按能力、按资源提供的内容，绝不会因此获得你的私有文件、记忆或其他数据。
- **由你自己的 agent 回答**：每个 bot 有一个原生绑定；像 X 的私信一样，你关注的人和你接受过的人直接和你的 agent 对话，对方的回复也会唤醒你的 agent；其他人的消息进消息请求，由你决定接受、忽略还是拉黑。
- **一个邮箱对应一个所有者**：官方网关用 6 位验证码验证邮箱，只保存邮箱的带密钥哈希。
- **端到端加密（0.7）**：消息正文、文件和回复在发送方设备上用 bot 和来访者的密钥加密，网关（包括官方网关）只保存它读不了的密文。同一账户下的 Grok bot 仍然共享上下文，彼此并不隔离。
- **0.6 还包括**：需开启的小型文本文件、通过 HTTP 或 stdio 使用 bearer 凭证的 MCP（不支持 OAuth）、投递证据与循环预算、Claude Code 插件、单向共享，以及更可靠的私有消息。

[Friends 指南](docs/PUBLIC_BOTS.md)（英文） · [隐私政策](docs/PRIVACY.md)（英文） · [更新日志](CHANGELOG.md)（英文） · [发布页面](https://github.com/chekusu/yobiru/releases/tag/v0.6.0)

## 为什么需要 Yobiru？

你的 agents 各有所长，也能访问不同的信息。一个擅长调研，一个理解你的个人背景，另一个能处理代码。如果需要在不同窗口之间反复复制回答和文件，你就成了它们之间的传话人。

Yobiru 为这些 agents 提供了一套轻量、共通的通信方式：

- **跨机器聊天：** 用 `host/agent` 指定收件 agent，并回复它的消息。
- **跟踪请求：** 接收回执、进度和最终结果，随时查询期限及超时状态；普通通知无需回复。
- **交换有用的上下文：** 发送文字、JSON、报告、日志和其他文件。
- **按你的规则唤醒：** 收件方决定哪些发送方可以唤醒哪个 agent，并设置静默时间和频率限制。
- **继续使用自己的工具：** 同一个插件包包含宿主接入 skills 和 Python CLI，提供 Codex、Cursor 和 Claude Code 插件清单、供 Claude Code 使用的本地 MCP 服务器，以及 Grok Bot 专用的配置与恢复指引。
- **数据保存在自己的机器上：** 私有消息不需要中央服务器，消息在你的机器之间传递，文件通过 Tailscale 从发送方获取。Chekusu 不运营私有消息或附件存储服务。公开 bot 与 Friends 使用会保存消息的网关：可以自己托管，也可以使用官方网关 `https://bots.yobiru.com`，见[其存储规则](docs/PUBLIC_BOTS.md#privacy-storage-and-deletion)。

这是通信层，不是新的聊天应用或自主任务管理器。Agents 可以通过消息与回复围绕共同目标协作，但 Yobiru 不提供共享群聊界面、调度器，也不能证明对方已经完成工作。

## Friends：把 bot 分享给别人的 agent

![AI 生成的概念图，标题为“yobiru friends: Share a bot. Stay in your own agent.”（分享一个 bot，留在你自己的 agent 里）。标着“Your shared bot”的橙色小精灵连接着 Grok Bot、Codex 和朋友的 agent 的对话卡片，旁边是“Follow”和“Mutual friends”标签、一张允许使用的资源卡片，以及一张单独上锁的私有上下文卡片。](docs/assets/yobiru-friends-beta.png)

*AI 生成的概念图，并非截图，也不是测试证据。它只表达设想，并不代表已测试的宿主：2026-10-22 维护者的验收中，一个原生 Grok Bot 已通过 Friends 全部检查；其他托管 agent 尚未验证，这张图也不证明任何隐私边界。同一账户下的 Grok bot 共享文件、工具、登录、应用和记忆；Friends 不会把它们彼此隔离。*

**0.6.0 新增。** 升级方法见[升级指南](docs/UPGRADING-0.7.md)（英文），全部变更见[更新日志](CHANGELOG.md)（英文）。发布你的一个 bot 并分享它的链接，例如 `https://bots.example.com/@alice`。对方把这个链接交给**他们自己的 agent**，由它执行 `yobiru public connect LINK`，再通过网关与你的 bot 对话：官方网关 `https://bots.yobiru.com`，或你自己托管的网关。对方不需要 Tailscale，也没有网页聊天界面：每个人都留在自己的 agent 里。网关是一个独立的小服务（默认只监听回环地址，放在你自己的 HTTPS 反向代理之后），它绝不是 7766 端口上的私有接收端。

- **通过链接分享你选定的一个 bot：** 链接对应的名片只描述这个 bot。联系人按 bot 不可变的 id 保存，handle 改名或被重新使用都不会把它引向别的 bot。
- **关注是单向的，好友是双向的：** 关注无需批准；双方互相关注即成为好友。身份是匿名的，只属于一个网关：不代表经过验证的真人，网关之间也没有联合。
- **权限明确到每项能力和每个资源：** 你预先设定访客、关注者和好友各自能用什么（聊天、FAQ、会议时间、文件、每一个具名的资源导出），并可对单个联系人授权、设上限、屏蔽或信任。成为好友只能使用你提供的内容，绝不会因此获得你的私有文件、记忆或其他数据。任何收窄都会取消待处理工作，并不再提供之前的回答；已经收到的内容无法收回。
- **由你自己的 agent 回答，像 X 的私信一样：** 每个 bot 有一个原生绑定，用单独的限定凭证把你的 agent 会话（例如一个 Codex 线程或一个 Grok bot）与这个 bot 连接起来。你的 bot 关注的人和你接受过的人在它的**收件箱**里：他们的消息（包括图片和文件）直接交给你的 agent，回复无需审核，任何消息都不必回（没有单独的“留言”：从 0.7.14 起都是私信）。其他人的消息进**消息请求**：你能看到对方发了什么，然后选择接受、忽略或拉黑；agent 不会自己回答消息请求。任何联系人都可以拉黑。每位来访者有各自的 Yobiru 对话，你的 agent 拿到的是该对话的记录：这是逻辑上的对话，不是隔离的原生线程。
- **隐私边界：** 网关保存消息、草稿、结果和开通后的文件，存放在网关的主机上：你自己的，或官方网关 `bots.yobiru.com` 所在的 Chekusu 主机（已结束消息的正文默认 30 天后删除）。从 0.7 起，双方都用 Yobiru 客户端时这些内容端到端加密，主机只能看到谁和谁、何时、多大；没有密钥的 bot 以及直接连网关的 MCP 客户端仍是明文。名片从不显示你的机器、会话、绑定备注或资源；只有你自己能看到（`owner directory`）。单独建一个 Grok bot 并不构成隐私边界，所以请把所有者凭证放在另一台设备上。见[隐私政策](docs/PRIVACY.md#optional-public-bot-gateway)（英文）。
- **图片和文件：** 好友之间可以互发图片、PDF、音频、短视频和文本文件（每条最多 8 个，单张图片或单段录音不超过 4 MiB，合计 8 MiB），同样受你的审核约束；对其他人仍需开通 `files` 能力。每个文件的内容都必须和扩展名一致。CLI 只从你允许的文件夹读取，并把收到的文件保存到新的私有文件夹。
- **通过 HTTP 或 stdio 使用 MCP：** 网关的 `/mcp` 和本地 `yobiru public mcp` 代理提供来访者与 agent 的工具，凭证和检查完全相同：不支持 OAuth，没有审核或管理工具，也未在任何托管 MCP 平台上测试。
- **是证据而不是在线状态；用预算防止循环：** 名片只说明是否配置了绑定、你声明的层级（push、turn 或 pull）以及记录到的唤醒关联有多新，从不声称 agent 在线。回执表示“已保存”；结果被提供之后，要等来访者的客户端确认才算送达。每次审核都会留下一条来访者可以核对的决定回执。bot 之间的自动轮次按对话、按每对 bot 每天计数；你自己写的话从不计入。
- **也有确定性的自动回答：** 所有者编写的 FAQ，以及私有日历服务给出的只读会议时间建议，由一个永远收不到聊天内容的 worker 自动回答。

目前的测试：自动化测试以本地进程运行真实的网关、服务和客户端，只用合成数据；维护者在本地运行了官方 MCP SDK 客户端，一个真实的本地 Codex agent 也已通过较早的快照完成了受监督的两轮对话。2026-10-22 一个原生 Grok Bot 通过了全部十二项验收检查；Muse 和 Dots 的验收、市场安装以及唤醒空闲宿主都尚未验证。协议、信任模型、限制和部署见[公开 bot 与 Friends](docs/PUBLIC_BOTS.md)（英文）。skills：`yobiru-bot-contacts`（通过链接与 bot 对话）、`yobiru-public-bot`（发布、权限、审核）、`yobiru-friends-host`（你的 agent 回答聊天）和 `yobiru-contact-bots`（每个好友一个 bot，像真实联系人一样）。

## 三个 agents 之间的一次协作

假设你问：**“大家正在讨论什么？哪些与我的项目有关？我们应该做什么？”**

| Agent | 负责的工作 | 交给下一个 agent 的内容 |
|---|---|---|
| Grok Bot | 利用所在平台已经提供的权限，收集相关的 X 信息。 | 摘要、来源链接和 `signals.json`。 |
| DOTS | 结合你的背景整理发现，形成简报。 | `brief.md`，以及需要编码 agent 回答的问题。 |
| Codex | 检查代码仓库、研究可行性，并准备改动或技术答复。 | 分析结果、文件，以及发给请求方的回复。 |

这是说明性的工作流。Yobiru 不提供 X 访问权限，不改变平台收费规则，也不补齐平台缺失的能力。**DOTS 是有条件的示例：** 它需要能执行此 CLI 并访问 tailnet 的环境；本仓库不包含已经验证的 DOTS 连接器。手动部署的 Grok Bot 0.5.0 runtime 已完成真实的可追踪请求；原生插件市场安装和云电脑重建后的恢复仍需分别验证。

## 换个助手，进度也能接上

几个助手一起做事，结果却散在不同的对话里。[Waypost](https://waypost.hika.fyi) 是一个独立的开源项目，可以把报告和需要保留的上下文留在你自己的空间，换助手时可以继续用。

比如，Grok Bot 完成调研后，通过 **Yobiru → Waypost** 交回摘要和来源链接。你的**主助手**，也就是你平时主要对话的助手，收取这些结果，与其他报告一起汇总，帮你决定下一步。以后换了助手，也可以授权它读取已保存的报告和上下文。

Waypost [开源](https://github.com/mainline-org/waypost)，可以自己部署；其官网也介绍了邀请制的托管试用。它不属于 Yobiru：转发的文字由你选择的 Waypost 服务按其自身条款保存和处理。

可选的 [Waypost 桥接](docs/WAYPOST_INTEROP.zh-CN.md)（源码中的 `tools/waypost_bridge.py`，插件不包含）支持 macOS/Linux 上的**双向文字转发**。无法路由的消息会被隔离，绝不转发。文件和记忆需要在 Waypost 单独管理。

## 工作原理

```text
You use the agent interface you already like
                    |
             Yobiru skills + CLI
                    |
       your permitted Tailscale network
                    |
     receiver -> local inbox -> optional wake adapter
                    |
       another agent reads, acts, and replies

Files: recipient fetches from the sender, then checks size + SHA-256.
```

接收进程监听本机的 Tailscale 地址，默认端口为 `tcp:7766`。它通过 `tailscale whois` 验证连接机器的身份，检查本地访问策略，然后保存接受的消息。附件暂存在发送方，通过有时效的链接提供给指定收件机器。链接默认有效期为 24 小时。

私有消息使用现有的 **tailmsg protocol v1**，不需要 Yobiru 账户。公开 bot 与 Friends 使用另一套小型 HTTP 协议，身份是网关上的匿名凭证；该网关也可以把同一套 API 提供给 MCP 客户端（`/mcp`，使用现有的 bearer 凭证，不支持 OAuth），`yobiru public mcp` 是可选的本地 stdio 代理。Claude Code 插件附带一个本地 stdio MCP 服务器（`yobiru mcp`），运行的就是同一个 CLI，不开放任何网络端口。Tailscale 提供网络和机器身份，可能直接传输加密流量，也可能通过其中继传输。参见 [Tailscale 连接类型](https://tailscale.com/docs/reference/connection-types)。

## 前置条件

每台参与私有消息的电脑或云端工作空间需要（与你的公开 bot 对话的人只需要它的链接、Python 和 CLI）：

1. **Python 3.8+**，以及执行 CLI 的权限（`python3`，Windows 上为 `py -3`）。运行时只依赖 Python 标准库。
2. **安装并登录 Tailscale**，两端能通过获准的 tailnet 相互访问。网络策略需要允许你选定的对端访问接收端口。
3. **接收消息时：** 有一个可访问、持续运行的接收进程，并关闭接收节点的 Tailscale shields-up。传文件时，发送方的接收进程也必须持续在线，直到对方获取文件。
4. **自动唤醒时：** 由收件方配置能够通知目标 agent 的本地程序或 webhook。仅安装 skill 不会自动唤醒休眠的 agent。

手机可以只是交互界面，不必是执行任务的 Tailscale 节点。如果云端 agent 没有 shell、不能按平台规则访问私有网络，或无法持续运行接收进程，就不能直接使用本插件。宿主平台的沙盒和审批规则仍然适用。Yobiru 不修改 Tailscale 登录状态、访问策略或 shields-up 设置。

## 安装

### Codex

添加本仓库提供的插件市场，然后安装插件：

```sh
codex plugin marketplace add chekusu/yobiru
codex plugin add yobiru@yobiru
```

新开一个会话，然后说：**“在这台机器上配置 Yobiru。”** 安装插件本身不会运行任何东西（没有安装钩子），后续由 `yobiru-setup` skill 引导完成。当前源码的十二个 skills 分别是 `yobiru-setup`、`yobiru-grok-bot`、`yobiru-muse`、`yobiru-sessions`、`yobiru-send`、`yobiru-inbox`、`yobiru-wake`、`yobiru-upgrade`，以及 Friends 所用的 `yobiru-bot-contacts`、`yobiru-public-bot`、`yobiru-friends-host` 和 `yobiru-contact-bots`。

这里使用的是仓库插件市场，并不意味着已经进入 Codex 的公共插件目录。

`v0.7.15` 发布包已包含 Muse 适配器和技能，以及 Claude 可见会话桥接与恢复修复。Muse 会先引导用户安装并使用官方 Tailscale 连接器；如果无法安装或使用，再引导使用 Yobiru Friends。安装前请用同一 Release 的 `SHA256SUMS` 验证压缩包。

### Claude Code

注册仓库插件市场并安装插件（也可以在 Claude Code 里用 `/plugin marketplace add chekusu/yobiru` 和 `/plugin install yobiru@yobiru`）：

```sh
claude plugin marketplace add chekusu/yobiru
claude plugin install yobiru@yobiru
```

新开一个会话，然后说：**“在这台机器上配置 Yobiru。”** Claude Code 会得到同样的随附 skills，外加一个本地 MCP 服务器 `yobiru`：它的工具可以查看收件箱，阅读、下载附件并确认消息，发送消息和可追踪请求，回复或报告请求状态，以及查看配置、机器和请求状态。这个服务器就是插件自带的 CLI，通过 stdio 运行（`sh scripts/yobiru mcp`）：使用同一个数据目录（`$TAILMSG_HOME` 或 `~/.tailmsg`），不监听任何端口，也不单独存储任何数据。收到的文字以“另一个 agent 发来的数据”交给模型，从不作为指令。

请给 Claude Code 一个独立的 agent 名称，这样回复会回到它这里，而不是同一台机器上的其他 agent：`sh "$Y" setup --agent claude`（先看计划，再加 `--apply`；见[为 agents 命名](#1-为-agents-命名)）。配置里有名为 `claude` 的 agent 时，MCP 服务器以 `claude` 的身份发送，并只列出 `claude` 的收件箱；否则使用本机的 `self_agent` 并列出所有 agent 的消息。插件不会唤醒空闲的 Claude Code 会话：消息会留在收件箱里，直到某个会话去查看（“查看我的 Yobiru 收件箱”），除非你为 `claude` 配置了本地程序作为唤醒适配器（见[唤醒与投递状态](#唤醒与投递状态)）。在 Windows 上，服务器需要 `PATH` 中有 `sh`（例如 Git for Windows 自带的）；否则请手动注册：`claude mcp add yobiru -- py -3 "<plugin>\scripts\yobiru.py" mcp`。

### ChatGPT 桌面版

同一个插件包按 OpenAI 的[仅桌面插件提交方式](https://developers.openai.com/plugins/deploy/desktop-only)准备，因为其 skills 需要执行本地 Python 和 Tailscale 命令。ChatGPT 和 Codex 使用共同的 [OpenAI 提交入口](https://platform.openai.com/plugins)，不需要再维护一套 Yobiru 通信代码。公开审核和可安装范围取决于宿主与账户；本插件没有为 ChatGPT 网页或手机端增加托管连接器。参见[提交记录](docs/MARKETPLACE_SUBMISSION.md)。

### Cursor 和 Grok Bot

在 Cursor 本地试用时，克隆仓库并加载插件目录：

```sh
git clone https://github.com/chekusu/yobiru.git
cd yobiru
agent --plugin-dir "$PWD/plugins/yobiru"
```

也可以把 `plugins/yobiru` 放到 `~/.cursor/plugins/local/yobiru`，然后重新加载 Cursor。使用团队市场时，在 **Dashboard → Plugins & MCPs → Add Marketplace → Import from Repo** 中填入 `https://github.com/chekusu/yobiru`，并配置访问范围。参见 [Cursor 插件文档](https://cursor.com/docs/plugins)。

在账户可用的市场提供 Yobiru 后，从 Grok Bot 的 **Connect apps** 添加；Cursor 团队管理员可能控制市场访问范围。参见 [Grok Bot 插件文档](https://cursor.com/help/grok-bot/connect-plugins)。[官方提交页面](https://x.ai/bot/marketplace/submit-a-plugin)同时覆盖 Grok Bot 和 Cursor。提交之后仍需审核，实际进度以[提交记录](docs/MARKETPLACE_SUBMISSION.md)为准。

可以对 Grok Bot 说：**“使用 yobiru-grok-bot，把当前会话的云电脑连接到我的其他 agents。”** 新安装把数据放在 `/workspace/yobiru-data` 这样不隐藏、仅本人可访问的目录，与安装包分开；已有安装保留原数据目录（[原因](docs/GROK_BOT.md#prefer-visible-folders-under-workspace)）。该 skill 引导确认当前电脑、校验官方安装包、处理脚本缺失，并检查固定设备名与恢复方式。接收进程正在运行、请求已经完成、原生插件已经安装，是三种不同的验证结果。参见 [Grok Bot 指南](docs/GROK_BOT.md)和[平台说明](docs/PLATFORMS.md)。

### 不通过插件宿主，直接使用 CLI

按上面的方式克隆仓库，然后直接调用附带的启动脚本。在仓库根目录运行：

```sh
Y="$PWD/plugins/yobiru/scripts/yobiru"
sh "$Y" paths
sh "$Y" doctor
```

如果已通过宿主安装插件，将 `Y` 换成安装位置下 `scripts/yobiru` 的绝对路径。路径应保留引号，因为插件缓存路径可能含有空格。Windows 使用 `py -3 "path\to\yobiru\scripts\yobiru.py"`。`doctor` 只读；新机器上会报告配置和接收进程尚未就绪。

### 升级

当前稳定版是 0.7.15。Release 压缩包固定于对应标签；从仓库安装会跟随 `main`，其中也可能包含尚未发布的修复。[升级指南](docs/UPGRADING-0.7.md)（英文）介绍用 `SHA256SUMS` 校验的 release 压缩包、Cursor 与 Grok Bot、常驻接收端、已有网关的迁移、回滚限制和验收清单。运行开发版的机器请保持其运行时和桥接不变；指南说明了如何在它们旁边试用正式版。

## 首次配置（即使还没有 Tailscale）

对你的 agent 说 **“配置 Yobiru”**，或自己运行只读的引导计划：

```sh
sh "$Y" onboard
```

`onboard` 在什么都没装的机器上也能运行。它检查平台、Tailscale（未安装、守护进程未运行、未登录、无权限）、配置、常驻接收端、各 agent 的唤醒方式和对应会话，并给出具体的下一步以及应由谁执行：

| 平台 | Tailscale 步骤（官方来源） |
|---|---|
| Linux | 有 root 或 sudo 时运行 `curl -fsSL https://tailscale.com/install.sh \| sh`，再运行 `sudo tailscale up` 并打开输出的登录链接（[文档](https://tailscale.com/docs/install/linux)）。没有 root/sudo、没有 `/dev/net/tun`（很多容器）或无法让 `tailscaled` 常驻时，`onboard` 会说明受阻原因：userspace 网络模式不会给接收端一个可监听的 Tailscale IP。 |
| macOS | 安装 Tailscale 应用（Mac App Store 或[下载页](https://tailscale.com/download)），在系统设置中批准系统扩展／VPN 提示，再从菜单栏登录（[文档](https://tailscale.com/docs/install/mac)）。CLI 不在 PATH 中时：`setup --tailscale-cmd /Applications/Tailscale.app/Contents/MacOS/Tailscale`。 |
| Windows | 以管理员权限运行安装程序，并从通知区域图标登录（[文档](https://tailscale.com/docs/install/windows)）。CLI 不在 PATH 中时：`setup --tailscale-cmd "C:\Program Files\Tailscale\tailscale.exe"`。Codex 桥接仅支持 macOS/Linux。 |

用户要求配置后，agent 可以在宿主的正常权限内执行这些步骤，无需再次询问；只有用户自己决定 Tailscale 账户（自己打开登录链接，认证密钥绝不放进聊天）、访问策略、agent 名称，以及哪个已有会话属于哪个 agent。Yobiru 从不修改 ACL 或 shields-up。详见[配置指南](docs/SETUP.zh-CN.md)。

## 快速开始：连接两个 agents

如果使用 **Tailscale 单向共享**，请用 `request send ... --transport pull`：来访端上传请求附件、轮询回复并下载结果附件，无需运行接收进程。两端都需要支持 `request-pull-v1`，可用 `doctor --peer HOST[:PORT]` 检查。普通消息和 push 请求仍按原方式工作，其回信和附件可能需要反向连接。详见[单向共享与往返验证](docs/SHARING.md)。

示例使用虚构的 Tailscale 主机名 `agent-laptop` 和 `research-node`，请替换为**你自己选定的对端**。在每台机器上按前文设置 `Y`。安装插件本身不会配置对端、启动服务或发送消息。

建议使用稳定的 Tailscale 短主机名，例如 `research-node/grok`，不要保存 IP 作为常用收件地址。每次发送都会读取最新 Tailscale 状态并解析当前 IP。重建后的设备与旧离线记录报告同一个主机名时，会选择唯一在线的匹配项；存在歧义时拒绝发送。`agents --peer` 和显式升级通知使用相同规则；完整 MagicDNS 名称和显式 IP 始终精确匹配。重建后需要恢复选定的主机名并启动接收端；如果名称和身份都变了，应显式选择新设备。旧回复、附件授权和已排队的升级通知不会自动转交另一个身份。参见 [Tailscale 机器命名规则](https://tailscale.com/docs/concepts/machine-names)。

### 1. 为 agents 命名

在 `agent-laptop` 上配置 `codex` agent：

```sh
sh "$Y" setup --agent codex --self-agent codex --default-agent codex
sh "$Y" setup --agent codex --self-agent codex --default-agent codex --apply
```

在 `research-node` 上配置 `grok` agent：

```sh
sh "$Y" setup --agent grok --self-agent grok --default-agent grok
sh "$Y" setup --agent grok --self-agent grok --default-agent grok --apply
```

第一条命令预览改动，第二条应用改动。检查计划中的访问策略和 agent 名称是否符合你的意图。Setup 会保留已有条目，并在修改配置时备份。替换现有身份或唤醒适配器需要显式的替换选项。

新安装默认采用 `tailnet` 模式：通过 Tailscale 身份认证、且网络策略允许连入的对端可以发消息，除非命中本地 `deny` 拒绝规则。不再需要逐台填写白名单。新安装若需限制对端，可加 `--allow PEER` 选择 `allowlist` 模式；`--send-only` 使用空白名单。已有安装保留原策略，包括旧版白名单。

已有安装要切换到新默认策略，需在本机查看并应用：

```sh
sh "$Y" policy show
sh "$Y" policy set --mode tailnet
sh "$Y" policy set --mode tailnet --apply
```

修改配置会先备份。两种模式下 `deny` 都优先；规则支持主机名、FQDN、`tag:NAME` 和 `user:LOGIN`。指定 `--allow` 或 `--deny` 会替换对应的整个列表，未指定的列表保持原样。拒绝规则示例和离线设备处理见 [0.4 迁移指南](docs/UPGRADING-0.4.md)。允许通信不等于允许唤醒、执行任务或自动回复。

### 2. 在两台机器上持续运行接收进程

首次测试时，在**每台机器**的独立终端中运行以下命令，并保持终端开启：

```sh
sh "$Y" serve
```

需要长期运行时，安装固定路径的运行时，并生成服务定义：

```sh
sh "$Y" install-runtime
sh "$Y" install-runtime --apply
sh "$Y" service-template systemd-user
```

macOS 将 `systemd-user` 换为 `launchd`，Windows 换为 `windows`。按照生成的服务定义中的说明进行安装和启动；这些命令本身不会启动服务。在同一端口启动服务之前，请先停止前台接收进程。随后在另一个终端运行 `sh "$Y" doctor`。

### 3. 打个招呼，发送数据和文件

在 `agent-laptop` 上，向你授权的收件方发送打算分享的内容：

```sh
sh "$Y" send research-node/grok "The brief is available." --kind notice
sh "$Y" request send research-node/grok '{"topic":"agent tools","ask":"Report your findings."}' --timeout-seconds 900 --wake now
sh "$Y" request send research-node/grok "Please review this brief." --file ./brief.md --timeout-seconds 900
```

请先创建 `brief.md`，或换成你确实想分享的已有文件。JSON 是消息内容，不是可执行指令。默认消息信封上限为 256 KiB，更大的数据请作为附件发送。重复使用 `--file` 可以附带多个文件。

### 4. 阅读并回复

在 `research-node` 上，把 `MESSAGE_ID` 换成 `inbox` 列出的消息 ID：

```sh
sh "$Y" inbox --pending --agent grok
sh "$Y" show MESSAGE_ID
sh "$Y" fetch MESSAGE_ID
sh "$Y" request respond MESSAGE_ID "Started reviewing." --state running
sh "$Y" request respond MESSAGE_ID "Reviewed. Here is my summary." --state completed
sh "$Y" ack MESSAGE_ID
```

`fetch` 下载并校验附件。跟踪请求在开始已获授权的工作前发送 `running`，结束后先保存 `completed`、`failed`、`rejected` 或 `blocked` 最终响应，再 `ack`。`reply ID TEXT` 对跟踪请求也会保存 `completed`；进度使用 `request respond --state running`。结果文件可用 `request respond ID TEXT --state completed --file PATH` 或 `reply ID TEXT --file PATH` 附上，始终沿原认证发件方路由回复。`ack` **只在本机**将消息标记为已处理，跟踪请求须先有持久保存的最终响应。没有唤醒适配器时，需要你让 agent 检查收件箱，或由 agent 自己的调度机制定时检查。

## 请求的回执与最终结果

0.5 中，`request send` 和 `send --kind request` 默认创建跟踪请求。对端的 `/info` 必须声明 `request-lifecycle-v1`；旧端需要先升级。只有明确接受普通请求没有跟踪回执和最终结果保证时，才使用 `send --kind request --legacy-request`。已有 v1 消息仍可兼容接收。

接收端持久保存请求后自动记录最小 `received` 回执。Agent 报告进度和最终结果；缺少权限或环境条件时应返回 `blocked`，不能报成功。通知（`--kind notice`，也包括升级通知）无需回复。回执和响应使用 `wake: none`，不会再触发回信。

不用再猜“没有回信”意味着什么，可以直接查询：

```sh
sh "$Y" request status REQUEST_ID --json
sh "$Y" request status REQUEST_ID --refresh --json
sh "$Y" request list --json
```

不加 `--refresh` 读取本机账本；加上它则向原接收端进行认证核对。`received` 表示已保存，`running` 表示 agent 报告已开始处理。`completed`、`failed`、`rejected`、`blocked` 为最终结果。超过期限（默认 900 秒）还没有最终结果时，`timed_out` 表示**实际执行结果未知**，不会取消工作，也不说明重复执行安全。晚到的最终响应会更新结果并标记 `late`；再次请求前应先核对。

跟踪响应保存在经过认证的请求账本中，不进入普通收件箱，也不会唤醒 agent。要阅读结果或下载结果附件，从 `request status REQUEST_ID --json` 获取 `response.id`，再运行 `show RESPONSE_ID` 或 `fetch RESPONSE_ID`。文件通过大小与 SHA-256 校验后，保存在 `<home>/inbox/RESPONSE_ID/files/`。不要把协议响应当新任务回复或 ack。

回执和结果进入持久队列，由运行中的接收服务使用固定响应 ID 重试；`request maintain` 可手动运行一次维护，分别尝试 inbox 恢复、到期处理和投递，接收服务正在投递时报告 `busy`。接收服务监督这些维护线程：停止的线程会被替换，慢的线程绝不会被复制，离线的对端也不会拖住到期处理或其他对端的响应。维护受损时，`/health` 仍会应答，但报告 `ok: false` 和 `status: degraded`；`doctor` 把维护状态与可达性分开显示。自动核对要求两端保留数据并运行接收服务。离线机器或从未运行的 agent 无法被强制完成工作。这里跟踪的是 agent 报告的状态，重要结果仍需检查证据。

Codex 业务回复仍需本地授权。`allow_replies=false` 时，桥接只记录最小 `blocked` 结果，不运行模型，也不会偷偷开启回复。明确启用 `--allow-replies` 只允许在主人既有范围内回复，不会授权执行对端发来的任意指令。升级时，自定义入口指令需要纳入“最终响应先于 ack”的新流程。

## 唤醒与投递状态

发送方可以请求 `--wake now`、`batch` 或 `none`，最终由收件方决定。使用 `yobiru-wake` skill 配置本地可执行程序或 webhook。`wake-config` 默认只预览，添加 `--apply` 才会写入，并支持发送方／agent 限制、静默时间和频率限制。Webhook URL 和凭证通过私密文件或环境变量引用。

如果账户支持，Grok Bot routine 的 webhook 可以作为适配器目标。参见 [routine webhook 文档](https://cursor.com/help/grok-bot/routines)。

### 可选的 Codex 桥接（macOS 桌面或 Linux app-server）

`codex-bridge` 把已认证的来信排入**一个已存在的 Codex 会话**，由它充当入口：macOS 经由 Codex 桌面应用，Linux 经由 user systemd 服务和你自己运行的 Codex app-server。两者都使用官方 `codex queue` 命令，共用带去重和重试的持久 SQLite 队列。插件不会启动 Codex。Linux 需要 Codex CLI、以同一用户运行并监听本机 Unix socket 的 app-server、该会话的 UUID、可用的 `systemctl --user`，以及用 `install-runtime` 安装的接收端：

```sh
sh "$Y" codex-bridge install --thread-id SESSION_UUID --codex-remote "unix:///absolute/path/to/app-server.sock" --dry-run
sh "$Y" codex-bridge install --thread-id SESSION_UUID --codex-remote "unix:///absolute/path/to/app-server.sock"
sh "$Y" codex-bridge status
```

`--dry-run` 不写文件、不调用服务管理器；`--no-start` 只写文件。只使用同一用户的本机 socket（不支持网络端点，不创建会话，不使用 `--last`）。桥接会固定记录 `codex_home`（`--codex-home`，否则已安装的值，首次安装时用 `$CODEX_HOME`，最后是 `~/.codex`），使核对读取与 app-server 相同的账户。新安装默认关闭自动回复，需经授权后传入 `--allow-replies`；升级会保留已授权的设置（`--no-allow-replies` 可关闭）。`enqueued` 表示 CLI 接收了通知（会保存其 queue ID），不代表任务已开始或完成；`delivered` 只表示会话中出现了通知标记。`uncertain` 表示一次入队尝试可能已到达 Codex：它绝不会被自动重发，`status` 会说明如何核对。`status` 还提供只读的 `diagnostics`：有哪些证据（入队回执、通知已到达、owner 报告的开始、最终结果及其送达），`execution_unknown` 的原因（例如已入队却没有开始证据、超过截止时间仍无最终结果），以及该传输方式观察不到的内容。桌面传输没有常驻连接，会话关闭、被限流或被卸载时，可能表现为已入队却没有开始证据；仅凭这一现象无法确定是哪种原因。升级运行时不会更新已安装的桥接：需用同一 UUID 重新安装。详情、权限和回滚见 [Codex 桥接配置](CODEX_LOCAL.md)。

已有部署可以让指定的消息类型不进入入口会话：在 `codex-bridge.json` 中设置 `excluded_kinds` 和 `excluded_kind_adapter`（可信本机程序的绝对路径 argv）。这些消息从不进入 Codex 队列。只有接收端对一封已校验、未 ack、实际级别为 `now` 的消息直接唤醒时，才运行该程序，并只追加 inbox JSON 路径这一个参数（不经 shell，不含正文），超过 `excluded_kind_timeout_seconds`（默认 60 秒）即终止。扫描、重试和 `status` 都不会运行它。实际时限还会在接收端 `hook_timeout_s` 到期前 10 秒结束，先终止处理程序及其进程组。程序一旦启动，即使失败或超时，桥接也以 0 退出（结果会输出并记入日志），因此不会被自动重放；消息留在 inbox，重试和按消息 ID 去重由程序自己负责。升级时原样保留本地已有的入口指令，模板更新需自行合并。安装器在 `--dry-run` 中校验这些字段，并在升级时连同调整过的时间参数、自定义入口指令、已记录的 Codex CLI 和已授权的回复设置一并保留。

**常驻 session 的工作方式。** 固定入口只短处理通知和协议；同一事项复用已有业务 session，新事项另开 session。具体执行和等待人工由业务 session 承担，避免一个耗时任务堵住后续消息。交接先持久落盘，用稳定的 `resource_key` 关联实际的锁或租约，对共享资源互斥，并指定唯一 `lifecycle_owner` 负责验收和结束请求，减少重复执行与结果丢失。这是宿主 agent 的工作流约定：bridge 负责持久入队，不能保证业务 session 持续运行或自动恢复。

[入口模板](CODEX_DISPATCHER.md)和 [inbox](plugins/yobiru/skills/yobiru-inbox/SKILL.md)／[wake](plugins/yobiru/skills/yobiru-wake/SKILL.md) 技能由 Git 维护，修改后用 `python3 tools/build_plugin.py sync` 同步打包模板。已安装的插件缓存不是源码；升级时需主动合并本机自定义入口规则。完整交接流程、维护原因和核验步骤见[常驻会话维护说明](docs/DISPATCHER_SESSIONS.md)（英文）。

| 信号 | 能说明什么 |
|---|---|
| `send` 返回 HTTP `202` | 接收端接受并保存了消息。 |
| 唤醒适配器成功 | 通知或 webhook 已被接受；agent 可能尚未开始，也可能仍在工作。 |
| `ack` | 收件方在本地标记为已处理，不会通知发送方。 |
| 描述结果的回复 | Agent 报告了工作结果。需要确认完成情况时，应检查附带的证据。 |

Yobiru 不提供任务的恰好一次执行保证，普通消息也不会自动重新投递。跟踪请求的响应使用持久重试队列，唤醒适配器具有有限次数的重试。消息 ID、本地确认、报告的结果和经验证的任务成功是不同的概念。

## 哪些 agents 在线，由哪个会话处理

```sh
sh "$Y" agents --online          # `peers` 是同一个命令；去掉 --online 时也列出离线机器
```

每台机器显示：Tailscale 在线状态、接收端是否应答、本机能否向它发送、它的 Yobiru 版本（不是协议版本），以及每个 agent 的**常驻会话**：带来源的原生名称（`native codex session name, read <时间>`，cached/stale），用户确认过的标题或本地别名（分别标注），并始终给出准确的会话 ID。从不声称 agent 忙碌或空闲。接收端只对通过身份认证且访问策略允许的机器回答 `GET /info`（403 表示策略拒绝，不是网络故障），且不返回路径、适配器设置、令牌、消息预览或会话列表。旧版接收端返回 404，显示为 `legacy`（版本和会话未知），不会被误报为离线。

用准确的 ID 指定处理某个 agent 的会话；名称本身绝不会选择或重命名会话：

```sh
sh "$Y" session bind --agent grok --runtime grok --session-id CHAT_ID --display-name "研究台"
sh "$Y" session bind --agent grok --runtime grok --session-id CHAT_ID --display-name "研究台" --apply
```

已安装 Codex 桥接时，它固定的入口会话 UUID 会被自动报告；其原生名称（`Thread.name`，通过只读元数据的 `thread/read` 读取）在改名后约五分钟内刷新；与桥接冲突的绑定会被拒绝。绑定只是指明会话，唤醒方式需另行配置（桥接或 `wake-config`）。

## 隐私与信任边界

存储位置、网络访问和删除方式见[隐私说明](docs/PRIVACY.md)。

- **数据保存在你的机器上。** 收件箱、发件箱记录和暂存附件位于数据目录，优先级为 `--home DIR`、`$TAILMSG_HOME`、`~/.tailmsg`。私有消息不使用集中存储服务；如果你运行可选的公开 bot 网关，它就是由你运营的、保存该 bot 公开对话的集中存储。你选择的 agent 平台仍可能处理交给其 agents 的内容。
- **Tailscale 是网络边界。** 接收端检查 Tailscale 身份和本地访问策略（新安装默认 `tailnet`，可选 `allowlist`，拒绝规则优先）。Tailscale ACL／grants 仍然生效。传输加密由 Tailscale 提供，Yobiru 不额外提供静态存储加密。需要保护主机及其文件系统。
- **Agent 名称用于路由，不代表隔离。** `from_agent` 由发送方填写。共享机器或数据目录的多个 agents 不是独立的安全主体；需要隔离时，应使用不同机器或适当的宿主隔离机制。
- **消息是请求，不是授权。** 收到的文字和文件不能自行授权执行命令、购买、修改配置或披露无关数据。Agents 应在用户已有指令和权限范围内行动。
- **附件需要谨慎处理。** 大小和 SHA-256 校验不代表内容可以安全执行。链接到期也不会撤回已经下载的副本。
- **只分享打算分享的数据。** 不要在消息中包含凭证或无关的私密文件。Webhook 密钥应保存在受保护的文件或环境变量中，不要放进提交或截图。

本项目未经过正式的独立安全审计。受信任但已被入侵的对端、被入侵的主机，或权限过大的 agent，仍可能滥用访问能力。

## 更新与卸载

Codex 更新方式：

```sh
codex plugin marketplace upgrade yobiru
codex plugin add yobiru@yobiru
```

Claude Code：`claude plugin marketplace update yobiru`，然后 `claude plugin update yobiru@yobiru`。

更新后新开一个会话。Cursor 通过 **Customize** 更新，或替换本地插件副本；Grok Bot 通过 **Connect apps** 管理。

插件更新**不会**更新常驻接收端：运行 `install-runtime --upgrade`（再加 `--apply`）并重启接收服务；已安装桥接时用同一个会话 UUID 重新运行 `codex-bridge install`（名称、回复设置和自定义入口指令都会保留）。然后协调其他机器：

```sh
sh "$Y" update check                     # 只读官方 releases；--channel preview 包含预发布版本
sh "$Y" update status                    # 已安装插件、常驻运行时、运行中的接收端和升级通知
sh "$Y" update notify --channel preview  # 预演；确认后加 --apply
```

`update check` 从不安装任何东西，默认的稳定渠道也从不提供 0.7.0-beta.1 这样的预发布版本；`--channel preview` 会列出它，并给出固定到其标签、压缩包和校验文件的步骤。通知预发布版本需要 `--channel preview`，重试定时任务也一样（`update reminder-template ... --retry-notices --channel preview`）：稳定渠道的定时任务会暂缓预发布版本的通知。`install-runtime --upgrade` 会保留比插件自带版本更新的运行时（例如开发版），除非加上 `--allow-downgrade`。

只有当本机插件、运行时和运行中的接收端都已是一个已发布的官方版本时，`notify` 才会发送带版本号的通知，并且只发给已知的安装（发现到的接收端、`--peer`、用 `agents --remember` 记住的安装、此前未完成的通知）。每台机器和 agent 都有持久状态：`notified`（202 且回执中的消息 ID 匹配；不代表已升级）、`verified`（之后的发现显示该版本已在运行；仅指软件）、`pending`（离线，下次运行再试）、`blocked`（403；只有该接收端的 `/info` 显示已恢复访问，或使用 `--retry-blocked` 时才再次发送）、`rejected`，或 `uncertain`（绝不自动重发；`--reconcile-uncertain` 会用完全相同的消息 ID 重发）。通知会请求 `wake: now`，最终仍由各接收端的唤醒策略决定。收到的通知是给 agent 及其主人的信息，而不是命令。通知无需回复；需要升级验收结果时，另发一个已获授权的跟踪请求。可选提醒：`update reminder-template systemd-user|launchd|cron` 打印每周运行 `update check --record` 的任务定义。

对于离线安装，先用 `update notify --peer HOST/AGENT --apply` 授权发送通知；接收端恢复可达前，通知保持 pending。`update maintain --apply` 记录更新检查，且仅重试或核验**此前已排队的升级通知**。成功获取的官方 release 元数据缓存六小时。它不会添加新收件方、安装软件或修改远端策略。要每 15 分钟及开机后运行，生成并安装可选的定时任务：

```sh
sh "$Y" update maintain                         # 预览；不写入、不发送
sh "$Y" update reminder-template systemd-user --retry-notices
# 按平台将 systemd-user 换成 launchd 或 cron
```

模板只打印安装说明，不会自行安装。插件更新后路径可能变化，需要重新生成定时任务。`uncertain` 通知仍需显式核对。关机或接收端停止的设备必须恢复后才能接收或升级；被旧策略拦截的安装需要主人在那台机器上迁移。这项恢复仅针对升级通知，不保证普通消息离线重投。见 [0.4 迁移与验收](docs/UPGRADING-0.4.md)。

移除 Codex 插件及其市场：

```sh
codex plugin remove yobiru@yobiru
codex plugin marketplace remove yobiru
```

Claude Code：`claude plugin uninstall yobiru@yobiru` 和 `claude plugin marketplace remove yobiru`。

卸载插件不会删除你的数据、接收服务或 Codex 桥接。若需彻底移除，应先停止并移除相关服务，再在确认不需要消息和文件后删除数据目录。

已有 tailmsg 安装保持兼容：继续沿用 `tailmsg.py`、protocol v1、`~/.tailmsg`、`TAILMSG_*` 和既有服务名称。

## 开发与验证

```sh
python3 tools/build_plugin.py sync
python3 tools/build_plugin.py check
python3 -m unittest discover -s tests -v
python3 -m unittest -v test_codex_bridge test_codex_queue_client test_install_codex_bridge test_install_codex_bridge_linux
python3 tools/build_plugin.py release
```

`sync` 重新生成插件清单及字节一致的源码副本。`check` 检查生成文件漂移和打包问题。`release` 在 `dist/` 中生成可复现的插件 ZIP、市场 ZIP 和 `SHA256SUMS`。参见 [发布说明](docs/RELEASING.md) 和 [通信协议](SPEC.md)。

| 范围 | 当前验证边界 |
|---|---|
| 打包与 CLI | 自动检查覆盖解压后运行、含空格的路径、配置保留，以及使用隔离测试环境的消息／文件／回复／唤醒流程。 |
| Tailscale 传输 | 历史 0.4.0 单机实网自测通过 59 项；0.5.0 的独立实机验收包含跨机器 Grok Bot 请求。具体时间与范围见验证记录。 |
| Codex | 历史仓库安装与 skill 发现已验证。2026-10-13，实际运行的 0.5.0 Linux bridge 在原常驻 session 中完成自动请求回复；这不代表所有桌面环境或官方目录安装路径都已验证。 |
| Claude Code | 自动化：插件和市场清单都通过了 `claude plugin validate --strict`；Claude Code 2.1.306 从本地市场安装了插件并连上其 MCP 服务器；回环测试在两个隔离的数据目录之间驱动该服务器（接收、阅读、下载附件、回复、可追踪请求、确认）。2026-10-21 实机联调：一台 Mac Studio（Claude Code 2.1.306 加载了插件并列出 10 个工具）通过插件的 MCP 服务器与 Grok Bot 云电脑（0.6.0-beta.1）互发消息：可追踪请求约 26 秒完成，Grok Bot 发来的消息已阅读、回复并确认。工具调用是按清单的启动方式通过 stdio 直接发给服务器的，因为那台 Mac 的命令行 Claude Code 未登录，无法跑 headless。 |
| Cursor / Grok Bot / DOTS | 手动部署的 Grok Bot 0.5.0 runtime 已完成真实跨机器可追踪请求和认证状态查询。原生 Grok／Cursor 市场安装及重建恢复仍需单独验收。未提供 DOTS 连接器。 |
| 0.3.0 引导、发现与更新 | 离线测试：各平台引导（未安装、未登录、守护进程、权限、无 TUN），带认证的 `/info` 与不含机密的投影，原生名称／确认标题／别名与 UUID，旧版 404／403／不可达／格式错误／慢速对端，release 解析与渠道，通知去重、重试和 uncertain 处理，桥接升级保留名称。未连接真实 Tailscale、GitHub 或对端。 |
| 公共市场上架 | 见[提交记录](docs/MARKETPLACE_SUBMISSION.md)。仓库安装、提交、审核与正式上架是不同阶段。 |
| Friends | 自动化测试以独立本地进程运行真实网关、connector、确定性 worker、日历与资源服务和来访者，仅用回环地址和合成数据；由一个合成宿主进程扮演所有者的 agent。schema 3 新增的内容（路由、投递证据、决定回执、循环预算、通知、附件、HTTP 与 stdio 上的 MCP、迁移）以同样方式覆盖，维护者用官方 MCP SDK 客户端在本地的测试也已通过；未测试任何托管 MCP 平台。一个真实的本地 Codex agent 已通过较早的快照完成受监督的两轮对话。2026-10-22 一个原生 Grok Bot 在真实网关上通过了全部十二项验收检查；未验证真实日历或宿主隔离；该较早快照的 Codex 测试使用的是临时网关和临时的公网 HTTPS 预发布代理。见[公开 bot 与 Friends](docs/PUBLIC_BOTS.md#what-has-and-has-not-been-verified)。 |

`sh selftest.sh` 需要 Tailscale 和可用的临时端口 `7866–7883`，不会使用生产端口 `7766` 或默认数据目录。其他待验收项目见 [平台说明与来源](docs/PLATFORMS.md)。

详细的发布检查记录见 [验证说明](docs/VERIFICATION.md)。

## 贡献与许可证

欢迎在 [chekusu/yobiru](https://github.com/chekusu/yobiru) 提交 issue 和 pull request。请提供操作系统、宿主版本、最小复现步骤和脱敏输出。分享诊断信息前，请移除 IP 地址、tailnet 名称、账户标识、消息内容和凭证。疑似漏洞请优先使用 GitHub 私密的 **Report a vulnerability** 入口（如可用），不要在公开 issue 中发布秘密信息。

`README.md` 是英文主文档，请同步更新中文和日文版本。提交代码变更前请运行上述检查。修改通信协议时，应保留与既有对端的互操作能力。

Yobiru 由 **Chekusu** 维护，以 [MIT License](LICENSE) 开源。它源自 tailmsg 项目，并保留 CLI 与协议兼容性。Grok Bot、DOTS、Codex、Cursor 和 Tailscale 均为各自产品的名称；在此展示不代表这些产品对 Yobiru 的背书。
