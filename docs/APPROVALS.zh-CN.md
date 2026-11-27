# 用你的 passkey 批准 agent 的工作

你的一个 agent 请另一个 agent 做只有你能批准的事时（比如 Grok Bot 请你 Mac 上的 Claude Code 部署网站，或 Claude Code
请另一台电脑上的 Codex 删除一个分支），你不该还得在终端里原样打一句“暗号”。从 Yobiru 0.7.30 起，你在手机上打开一个
链接，看清自己要批准的确切内容，用 Face ID、指纹或设备 PIN 确认，就完成了。这一步用一把 **passkey** 给指令签名，
私钥从不离开你的设备。干活的那台电脑在它的 agent 看到请求之前，用它早已信任的你的 passkey 公钥，自己验证签名。

别人做不出这个签名：其他 agent 不行，声称“你已同意”的消息不行，你 tailnet 里的其他人不行，转送链接的服务器也不行。

## 需要什么

- 干活的电脑（**接收端**）和发出请求的电脑都装 Yobiru 0.7.30 或更新版本。
- 一台支持 passkey 的手机、平板或电脑：iPhone 或 iPad（iOS 16+）、Android 9+、带 Touch ID 的 Mac、Windows Hello，
  或者一把安全密钥。任何能打开 `yobiru.com` 的新版浏览器。

## 1. 在每台干活的电脑上设置你的 passkey（一次）

对那台电脑上的 agent 说：**“帮我设置 Yobiru 主人 passkey。”** 它会运行 `yobiru approval setup`，给你一个链接。
在手机上打开，点 **创建 passkey**。从此这台电脑就接受你用它签的批准。

在你的其他电脑上也这样做一次。每台电脑各自保存它信任的 passkey 列表，谁也不能替别的电脑担保。你的手机会在
`yobiru.com` 下为每台电脑各存一把 passkey，签名时页面会自动挑对那一把。

想沿用一把已有的 passkey：请一台已经信任它的电脑上的 agent 给出你的**主人公钥文本**（`yobiru approval keys`），
再交给新电脑上的 agent：**“信任这把主人公钥：yobiru-owner-key:…”**。只用你从自己电脑上拿到的文本：
一台还没有任何主人公钥的电脑，会直接信任交给它的第一把。

## 2. 发出之前先批准

对要发出工作的 agent 说：**“请我批准让 mac-studio 上的 claude 从 main 部署网站，然后发过去。”** 它会运行

```sh
yobiru approval request mac-studio.example.ts.net/claude "Deploy the site from main"
```

并给你一个链接。页面上会显示：

- 哪台电脑上的哪个 agent 可以执行，哪台电脑可以发出请求；
- 你的指令，一字不差；
- 请求可以带的确切文件（名称、大小和指纹）；
- 批准有效多久、可以用于几个请求；
- 由你的哪把 passkey 签名。

批准后，agent 会把带着你签名的请求发出去（`yobiru request send … --approval ID`）。接收端只在以下条件全部满足时
才认：签名来自它信任的你的 passkey，内容正是那条指令，对象是那台电脑上的那个 agent，发件方是那台电脑，文件
完全一致，没有过期，也没有超过可用次数。然后它的 agent 会看到你的原话，并标明由它自己的电脑验证过。

默认一次批准有效一小时、只用于一个请求。想要更多可以直接说（“接下来 2 天内，最多 10 次”），上限是 7 天、100 次。

## 3. 接收端的 agent 来问你时

如果一个请求没有带你的批准，而那边的 agent 需要它，agent 不会让你打字确认。它会把请求回复为 **blocked**，并附上
一个链接（`yobiru approval for ID`）。打开链接，检查请求并批准，发件方就会带着你的签名重新发送。

## 4. Friends 上你自己的 bot 之间

从 Yobiru 0.7.31 起，在 `bots.yobiru.com` 这样的 Friends 网关上，你自己的 bot 之间也能这样做，完全不需要 tailnet。
比如你用另一个 bot **@grok** 给你的 bot **@claude**（Mac 上通过原生绑定回复的 Claude Code）发消息：“从 main 部署网站。”
Claude Code 不会只凭一条聊天消息就动手。它回复一个链接，这个链接经 @grok 回到你手上。打开它，页面会写明可以执行的是
@claude、发出请求的是 @grok，以及你那条消息的原文和附件。用 passkey 批准，再告诉 @grok 你签好了。Claude Code 在它自己的
电脑上验证签名，然后只把这件事做一次。

只需设置一次：对替那个 bot 回复的 agent 说 **“帮 @claude 设置我的 Yobiru 主人 passkey”**（在保存这个 bot 绑定的数据
目录里运行 `yobiru approval setup --bot @claude`）。每个签名只对应一条消息，并且必须和它到达时一模一样（文字、附件和
发件人），默认一小时后过期，除非你选更长。如果是别人的消息要求做实际的事，agent 仍然可以来问你，但链接只给你一个人，
绝不给发件人。

## 你的 passkey

- **查看一台电脑信任哪些：** “这台电脑信任哪些主人 passkey？”（`yobiru approval keys`）。
- **添加一把**（新手机、安全密钥）：在那台电脑上再运行一次设置。页面先创建新的 passkey，再请这台电脑已经信任的
  一把 passkey 批准添加。
- **删除一把：** “这台电脑不要再信任 ab12-cd34-ef56-7890 这把 passkey”（`yobiru approval remove ab12-cd34-ef56-7890`，
  即 `approval keys` 显示的指纹）。删除要用你的某把 passkey 签名批准，可以就是被删除的那一把。
- **一台电脑信任的 passkey 全丢了：** 在那台电脑上删掉 Yobiru 数据文件夹里的 `owner_keys.json`，再重新设置。

## 它能防什么，不能防什么

它证明：解锁你 passkey 的人，批准了这段确切的文字，给这台电脑上的这个 agent，在这些限制之内。它**不能**：

- 区分你本人和拿着你已解锁手机的人；
- 给 agent 新的权限：agent 仍只能做它的电脑本来就允许的事；
- 让含糊的指令变精确：请用 agent 无法曲解的话写清你的意思；
- 覆盖指令没写的事。消息的其余内容和附件仍是不可信数据；
- 防住电脑自己：控制着接收端电脑或其 agent 的人，可以改它信任哪些 passkey。它防的是这台电脑以外的一切。

设置第一把 passkey 时，谁打开设置链接就信任谁，所以请你自己要求设置、自己打开链接，并确认页面上写的是你预期的
那台电脑。agent 只在你直接要求时才设置，绝不因为某条消息要求就设置。此后，一台电脑信任的 passkey 每次变动都需要
你的签名。

这里用的 passkey 绑定在 `yobiru.com` 上，所以只有 `https://yobiru.com/approve/` 这个页面能请你的设备签名。
这个页面只是几份静态文件：不用账号，不做追踪，也没有第三方脚本。

## 隐私

你的私钥留在你的设备上。电脑只保存公钥，放在数据文件夹里的 `owner_keys.json`。

链接经由 Yobiru 网关上的一个投递点转送请求和你的回答（默认 `bots.yobiru.com`，你也可以用自己的网关）。两者都用
一把只存在于链接 `#` 之后那部分的密钥加密，而浏览器从不把那部分发给任何服务器，所以网关只保存它读不懂、也无法
悄悄改动的密文。投递点 24 小时后删除。你不需要 Friends 账号。

## 给 agent 和开发者

| 命令 | MCP 工具 | 作用 |
|---|---|---|
| `yobiru approval setup [--label NAME] [--key TEXT] [--bot @BOT]` | `yobiru_approval_setup` | 在本机设置或添加主人 passkey，返回链接。`--bot` 指定这个数据目录替哪个 Friends bot 回复。 |
| `yobiru approval request PEER/AGENT TEXT [--file F] [--expires 1h] [--uses 1] [--any-requester]` | `yobiru_approval_request` | 请主人批准一条指令，返回链接。 |
| `yobiru approval for ID` | `yobiru_approval_for` | 在接收端：为一个没带签名的请求生成让主人批准的链接。 |
| `yobiru approval for MSG_ID [--bot @BOT]` | | Friends bot 的 agent（在它的原生绑定数据目录里）：为这个 bot 收到的一条消息生成让主人批准的链接；`give_link_to` 和 `reply_text` 说明链接该给谁。 |
| `yobiru approval wait REF [--timeout S]`、`get REF` | `yobiru_approval_wait` | 取回答复：`signed`（带 `approval_id`）、`key_added`、`declined`，或仍在等待。`get MSG_ID` 给出一条 Friends 消息的结论，每次都重新检查。 |
| `yobiru approval list` | | 还在等主人的链接，以及已有结论的 Friends 消息。 |
| `yobiru approval keys` | `yobiru_approval_keys` | 本机信任的主人公钥，含可分享的公钥文本。 |
| `yobiru approval remove FINGERPRINT` | | 请主人签名删除一把公钥。 |
| `yobiru approval verify ID` | | 像接收端一样在本机检查一个批准。 |
| `yobiru request send PEER TEXT --approval ID` | `yobiru_send` 带 `approval` | 发送带批准的跟踪请求。 |

在接收端，`yobiru show ID` 会把它自己的结论显示为 `received.owner_approval`；只有 `verified: true` 且
`authorization_status: active` 时才算，`inbox --json` 用 `owner_approved` 给出同样的结论。Claude Code 桥接会把
已验证的批准列在会话的启动提示里。发件方的说法、信封里自带的 `owner_approval` 字段或 `verified: false`，都不是批准。

协议细节（意图格式、WebAuthn 检查、接收端账本）见 [SPEC §10](https://github.com/chekusu/yobiru/blob/main/SPEC.md)；网关的投递点接口见
[PUBLIC_BOTS.md](PUBLIC_BOTS.md#approval-drops)（英文）。

在 Friends 上，批准是接收方 agent 自己的记录：`approval get MSG_ID` 只有 `verified: true` 才算，意思是这台电脑用这里
保存的主人公钥验证过签名，而且签的正是这个 bot、这条消息、它的原文和附件、这个发件人。网关给的 `from.same_owner` 只决定
链接给谁，从不批准任何事。给别人的 bot 签批准、或者让消息自己带着批准发过去，目前都不支持。
