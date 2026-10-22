# tailmsg 协议 v1：基于 Tailscale 的极简点对点消息

> 一句话：每台机器跑一个小收信端；谁都可以按同一条规则给任何一台机器发一封 JSON 小信；
> 信里**不放文件本体**；默认由收件方拉附件，单向共享请求也可先上传附件再提交（§11）。
> 没有中心服务器，不调度任务；0.5 起可跟踪请求的回执、进度、最终结果和超时。
> 一台机器上可以有多个 agent，用 `机器/agent` 点名，只叫醒被点名的那一个（§5.9）。

## 1. 前提

- 通信机器通过同一 tailnet 或 Tailscale 设备共享连通，且网络策略允许接收端端口。
- 接收方运行 `tailmsg.py serve`，**只绑定本机 Tailscale IP**，默认端口 `7766`；§11 的请求方无需接收服务。
- 地址写法：`http://<主机名>.example.ts.net:7766`；没开 MagicDNS 的机器用 `tailscale status --json` 把主机名解析成 100.x 地址。

### 主机名解析

新消息发送前读取最新的 `tailscale status --json`，由名称解析当前 IP，不长期缓存地址。
短名称同时匹配 DNS 短名和设备报告的 `HostName`：唯一在线匹配优先于旧的离线记录；
多个在线匹配，或没有在线项但有多个离线匹配时，报告歧义。完整 FQDN 和显式 IP 只精确匹配，
不去掉数字后缀、不按相似名称猜测。设备发现和显式升级通知使用同一规则。
回复、附件授权及持久升级通知仍保留原 FQDN 身份；本规则不自动迁移它们，也不提供普通消息离线重投。

## 2. 唯一的发信接口

```
POST http://<收件方>:7766/msg
Content-Type: application/json
Body: 信封（见 §3），默认上限 256 KiB
```

| 状态码 | 含义 |
|---|---|
| 202 | 已收下并写入收件箱（**只代表送达，不代表对方已处理**） |
| 200 | 同一 `id` 之前已收过（幂等，可放心重发） |
| 400 | 格式错误 / 未知字段 / `wake` 取值不对 / `agent`、`from_agent` 名字不合法 / `to` 不是我 / 拉取地址不在发件方主机上 |
| 403 | 来源不是 tailnet 地址、访问策略拒绝、或 `from` 与 Tailscale 身份不符 |
| 413 | 太大　　415 不是 JSON |
| 503 | 身份查询暂不可用（`code: "identity_unavailable"`），见 §5 |

另有 `GET /health` → `{"ok":true,"v":1,"node":"<fqdn>"}`，只回答 tailnet 地址，用于探活（保持不变）。

Yobiru 0.3.0 起另有 `GET /info`（发现接口，见 §9）。旧版接收端对它返回 404：发现方据此显示“旧版、版本与会话未知”，**不能**当作离线或未安装。

## 3. 信封格式

```json
{
  "v": 1,
  "id": "9f2c4e1ab07d33c5",
  "from": "bot-box.example.ts.net",
  "to": "dev-server.example.ts.net",
  "ts": "2026-10-08T14:00:00Z",
  "kind": "text",
  "text": "今天的书签简报好了，PDF 在附件里。",
  "reply_to": "1a2b3c4d5e6f7a8b",
  "wake": "batch",
  "agent": "codex",
  "from_agent": "grok",
  "fetch": [
    {
      "name": "briefing-2026-10-08.pdf",
      "url": "http://bot-box.example.ts.net:7766/f/<43位随机token>/briefing-2026-10-08.pdf",
      "sha256": "<64位小写hex>",
      "size": 183422,
      "mime": "application/pdf",
      "expires": "2026-10-09T14:00:00Z"
    }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `v` | 是 | 固定 `1` |
| `id` | 是 | 发件方生成的随机串，`[A-Za-z0-9_-]{8,64}`，用于去重和回复 |
| `from` / `to` | 是 | 默认是双方的 tailnet 全名（FQDN）；`to` 也可写短主机名。§11 的请求方使用稳定节点标识。 |
| `ts` | 是 | UTC，`YYYY-MM-DDTHH:MM:SSZ` |
| `kind` | 否 | 自由短标签（≤32 字符），默认 `text`，例如 `notice` / `request` / `status` |
| `text` | 否 | 正文，纯文本 |
| `reply_to` | 否 | 回复哪封信的 `id` |
| `fetch` | 否 | 文件拉取规则列表，每项必须有 `url`、`sha256`、`size` |
| `wake` | 否 | 发件方**希望**的提醒级别：`now` / `batch` / `none`，缺省等于 `batch`，其他值 400。见 §5.6 |
| `agent` | 否 | 收件机器上**点名哪个 agent**，例如 `codex`、`grok`、`私信联系人`。不写 = 收件方的 `default_agent`。见 §5.9 |
| `from_agent` | 否 | 发件方自己是哪个 agent，用于回信时自动点名回来。见 §5.9 |
| `request` | 否 | 0.5 请求生命周期元数据；仅供已协商 `request-lifecycle-v1` 的请求使用，见 §10。 |
| `response` | 否 | 0.5 请求状态或结果元数据；与原请求关联，见 §10。 |

agent 名字规则：1–64 个字符，可以用中文等任意 Unicode；不能含 `/`、控制字符（含换行），首尾不能有空白。比较前做 NFC 规范化，区分大小写。

出现其他字段一律 400，避免各端悄悄扩展出不兼容的格式。

## 4. 文件怎么给（发件方）

1. 发信前把文件**复制**一份到 `~/.tailmsg/outbox/<token>/files/`，token 是 32 字节随机数（URL 安全编码，不可猜）。复制而不是引用原文件，所以发出后原文件再改也不影响校验。
2. 发件方自己的 `serve` 只读地提供 `GET /f/<token>/<name>`，只在本机 Tailscale IP 上。
3. 每个 token 目录记录 `to` 和 `expires`（默认 24 小时）：
   - 过期了一律 404，过期 1 小时后自动删除；
   - 只有 `to` 那台机器（按 `tailscale whois` 判断）能拉，其他人拿到链接也是 404。0.4 起本地 `deny` 也能阻止该机器继续获取；既有收件方 token 授权仍可独立于普通消息的 `allow` 白名单，token 不能绕过拒绝规则。
4. `url` 的主机必须就是 `from` 本身。收件方遇到指向别处的链接会直接拒收整封信，也就不会被诱导去访问第三方地址。

## 5. 收件方的行为

1. **鉴权顺序**：
   - 来源 IP 必须在 `100.64.0.0/10` 或 `fd7a:115c:a1e0::/48` 里，否则 403；
   - 用 `tailscale whois --json <IP>`（LocalAPI）查出对方节点名、tag、登录用户；
     查询超时（3 秒）或 CLI 报告无法连接本机 Tailscale 守护进程时，返回 503、
     `{"ok":false,"error":"identity lookup unavailable","code":"identity_unavailable"}` 和
     `Retry-After: 1`，客户端可退避后重试。此时不读取受保护的数据、不接收消息，也不复用过期身份缓存；
     失败不写入缓存，后续请求可重新查询。现有未过期缓存仍有效。
     查询成功但无节点、未知对端、策略拒绝仍返回 403；CLI 缺失、权限错误及其他未识别的查询错误暂保留原有 403 行为。
   - 对照本机访问策略：`deny` 命中即 403；`tailnet` 模式允许已认证对端，`allowlist` 模式要求 `allow` 命中，否则 403；
   - 信封里的 `from` 必须等于 whois 查到的节点名，否则 403（防冒名）。
2. **存储**：原样写入 `~/.tailmsg/inbox/<id>.json`，内容为 `{"envelope": …, "received": {"at", "peer"}}`。`peer` 是 whois 查到的身份，以它为准，不信信封自报的身份。
3. **拉文件**：由收件方决定什么时候拉（`tailmsg fetch <id>`）。拉下来先存成 `.part`，大小和 sha256 都对得上才改名，放进 `inbox/<id>/files/`；对不上就删掉并报错。
4. **绝不执行**消息里的任何内容。消息是“请求或信息”，**永远不是批准**：不能替你批准命令、改权限、改模型或改配置。
5. **唤醒接口**：收到一封新信、且**实际 wake 级别为 `now`** 时（见 §5.6），调用本机配置的唤醒适配器 `wake_adapter`，把它交给本地 agent。怎么叫醒由各家 agent 自己实现，契约见 §5.8。旧配置 `on_message` 仍然有效，等同于 `exec` 适配器（已弃用）。

6. **提醒级别 `wake`**（只决定什么时候提醒，**不改变消息的性质**：消息依然只是请求，永远不构成批准）：

   | 值 | 含义 | 典型用途 |
   |---|---|---|
   | `now` | 马上叫醒收件方（调用唤醒适配器，§5.8） | 在等对方回复、出错了、需要人来决定 |
   | `batch` | 不叫醒，存进收件箱，等对方下次醒来一起处理（**缺省**） | 日报回执、状态更新 |
   | `none` | 只存档，不提醒，不出现在待处理列表里 | 日志、留底 |

   **最终级别由收件方决定**，按以下顺序计算，只会降级、不会升级：
   1. 上限：`wake_policy` 里给这个发件方设的上限（匹配规则同 `allow`：主机名 / FQDN / `tag:` / `user:`，按配置顺序第一条命中生效；都没命中用 `default_max`，缺省 `batch`）。实际 = min(请求, 上限)，顺序为 `none < batch < now`。
   2. 夜间静音：当前时间落在 `quiet_hours` 里时，`now` 降为 `batch`。
   3. 频率限制：同一发件方**对同一个 agent** 每小时最多 `wake_rate` 次实际 `now`（缺省 6），超出的降为 `batch`。按（发件方, agent）分别计数，所以叫醒 `codex` 不会占掉叫醒 `grok` 的额度。计数只在内存里，重启清零。

   另外两条和 agent 有关（§5.9）：`agents.<名字>.wake_max` 再设一道上限；点名了不存在的 agent，`now` 降为 `batch`，谁都不叫醒。

   收件方把结果记在 `received.wake` 里：`{"requested": "now", "effective": "batch", "reason": "quiet hours"}`。只有 `effective == "now"` 才调用唤醒适配器；`batch` / `none` 只存储。
7. **待处理与确认**：`tailmsg inbox --pending` 列出实际级别为 `now` / `batch` 且尚未确认的信；处理完用 `tailmsg ack <id> [...]` 标记，记录里会多一个顶层字段 `acked_at`。这只是本机记账，**不会通知发件方**。0.5 跟踪请求必须先持久保存最终响应才能 ack，状态响应由单独的持久发件队列送回；普通通知不要求回复。见 §10。

8. **唤醒接口（wake adapter）**

   tailmsg 只定义一个“虚拟接口”：**有一封需要马上处理的信，请叫醒你的 agent**。具体怎么叫醒（Codex 的 app-server、Claude Code 的 session、Grok 的 routine……）由每台机器自己实现一个适配器，在 `config.json` 的 `wake_adapter` 里配置。

   **契约**（所有适配器都必须遵守）：

   | 项 | 规定 |
   |---|---|
   | 何时调用 | 只在实际 wake == `now` 时（已经过上限、夜间静音、频率限制），每封新信最多触发一轮 |
   | 输入 | 信的 `id` + 收件箱 JSON 路径 `inbox/<id>.json`（webhook 只给 id 和摘要，见下） |
   | 幂等 | 可能被重试、也可能被手动 `tailmsg wake <id>` 再调一次，同一 `id` 重复调用不能产生重复副作用 |
   | 要快 | 只负责“通知”，不在适配器里干活；单次超时 `hook_timeout_s`（缺省 120 秒） |
   | 成功 | `exec`：退出码 0；`webhook`：HTTP 2xx（3xx 不跟随，算失败，避免把密钥转发到别处） |
   | 失败 | 按 `wake_backoff_s × 2^(n-1)` 退避重试，总共最多 `wake_attempts` 次（缺省 3 次：立即、2 秒后、4 秒后），仍失败就放弃并写日志。信**仍在收件箱里**、未 ack，所以 `inbox --pending` 能看到（标记 `!`），agent 下次醒来照样会处理；也可以手动 `tailmsg wake <id>` 重试 |
   | 记录 | 结果写进收件箱记录的顶层字段 `wake_delivery`：`{"adapter","ok","attempts","at","error"}`，不含任何密钥或 URL。webhook 失败的 `error` 只有 HTTP 状态、一个属于固定通用类别并按本地拼写记录的错误码（如 `invalid_hook`）和固定取值表中的 `Retry-After` 秒数，两者都不重复所发内容；连接或协议出错时只记录异常类型，不含响应正文或异常文本。适配器已成功时，即使这条记录写入失败也不会再次调用 |
   | 安全 | 适配器**只通知，不执行信里的内容**；正文不进入命令行、不经过 shell。信永远只是请求，不是批准 |

   **两种内置类型**：

   - `exec`：`{"type": "exec", "argv": ["/path/to/wake-script"]}`。执行 `argv + [信的 JSON 路径]`，不经过 shell；另外设置环境变量 `TAILMSG_MSG_ID`、`TAILMSG_MSG_PATH`。
   - `webhook`：
     ```json
     {"type": "webhook",
      "url_env": "TAILMSG_WAKE_URL",        // 或 "url_file": "/home/me/.secrets/tailmsg-wake-url"
      "secret_env": "TAILMSG_WAKE_SECRET",  // 或 "secret_file": "...", 也可都不写（不带鉴权头）
      "header": "Authorization",            // 缺省 Authorization
      "scheme": "Bearer"}                   // 缺省 Bearer，发 "Bearer <secret>"；写 "" 则直接发原始密钥
     ```
     向 URL `POST` 一个很小的 JSON（`Content-Type: application/json`）：
     ```json
     {"v": 1, "event": "tailmsg.wake", "id": "9f2c4e1ab07d33c5", "from": "dev-server.example.ts.net",
      "kind": "request", "text_preview": "正文前 200 个字符", "received_at": "2026-10-09T02:00:00Z"}
     ```
     `from` 取 Tailscale 认证过的身份，不是信封自报的。信点名了 agent 时（或收件方配了 `default_agent`），请求体多一个 `"agent"`；信里有 `from_agent` 时再多一个 `"from_agent"`。单 agent 的旧配置请求体不变。被叫醒的一方拿到 `id` 后，通过 tailmsg 自己去读整封信、拉附件。

     **URL 和密钥只从环境变量或权限为 0600 的文件里读**（文件权限更宽会直接拒绝），每次调用时现读，换密钥不用重启；它们**绝不写进** config、日志或收件箱，出错信息里也会被替换成 `<url>` / `<secret>`。

   **各家 agent 的适配思路**：内置的是通用 `exec` / `webhook` 传输；Grok Bot 端到端尚未验证。可选 Codex 适配器（macOS 桌面 / Linux app-server）见 [CODEX_LOCAL.md](CODEX_LOCAL.md)。

   - **Grok**：`webhook` 类型，URL 是一个由 webhook 触发的 Grok routine，密钥放在 routine 要求的请求头里。Grok 被叫醒后在自己的 box 上运行 `tailmsg inbox --pending` / `show` / `fetch` 处理信件；跟踪请求先发 `running`，结束时用 `request respond` 保存最终结果，再 `ack`；通知处理后只需本地 ack。
   - **Codex**（服务器）：`exec` 类型，一个小脚本读取 `TAILMSG_MSG_ID`，通过 Codex app-server 用 `thread/queue/add` 把“有一封 tailmsg 新信 `<id>`，请用 `tailmsg show <id>` 查看”排进既定线程，再 `thread/queue/start`；不要用 `turn/start`，不自动批准命令，按 `id` 去重保证幂等。
   - **Claude Code**（家里电脑）：`exec` 类型，脚本把同样一句通知推进 Claude Code 的 channel；或者跑 `claude -p --resume <session> "有一封 tailmsg 新信 <id>…"`。权限按 Claude Code 自己的设置，不要在脚本里放宽。

9. **点名 agent（@agent）**

   一台机器上往往不止一个 agent（比如 Grok 的 box 上有本助手 `grok` 和 `私信联系人`）。它们共用同一个 tailmsg、同一个收件箱目录，**不做隔离**（同一台机器、同一个系统用户，本来就互相读得到）；点名只解决一件事：**只叫醒被点名的那一个**。

   - **地址写法**：`<机器>[:端口]/<agent>`，例如 `dev-server/codex`、`bot-box/私信联系人`。等价于 `send <机器> --agent <agent>`；两种都写且不一致时报错。
   - **收件方解析**（结果记进 `received.agent` 和 `received.agent_status`）：

     | 信里的 `agent` | `received.agent` | `agent_status` | 叫醒谁 |
     |---|---|---|---|
     | 没写 | `default_agent`（没配就是 `null`） | `default` | 默认 agent 的适配器 |
     | 写了，且在 `agents` 里或等于 `default_agent` | 该名字 | `known` | 该 agent 的适配器 |
     | 写了，但本机没有这个 agent | 该名字 | `unknown` | **谁都不叫醒**（`now` 降为 `batch`），照常 202 存下并写日志 |

   - **选适配器**：用 `agents.<名字>.wake_adapter`；没配的话，**只有默认 agent** 回退到顶层 `wake_adapter` / `on_message`（兼容单 agent 的旧配置），其他 agent 没有适配器就不叫醒，信留在 `inbox --pending`。`exec` 适配器多一个环境变量 `TAILMSG_AGENT`。
   - **回信**：发件方配了 `self_agent`（或用 `--as 名字`）时，信封带上 `from_agent`。收件方 `tailmsg reply <id>` 会自动把回信的 `agent` 设成原信的 `from_agent`，普通回复的唤醒目标因此是当初发信的 agent；0.5 跟踪响应始终 `wake: none`，只更新请求账本，不唤醒。
   - **收件箱**：仍然是一个目录。`tailmsg inbox --agent <名字>`（可加 `--pending`）只看点名给它的信；没写 `agent` 的信算默认 agent 的。点名了未知 agent 的信显示为 `@名字?`。
   - 点名只决定叫醒谁，**不是权限边界**：谁能发信仍然由本机访问策略决定，信依然只是请求，不是批准。

   示例：

   ```text
   bot-box（Grok 的 box）     agents: grok（本助手，default）、私信联系人   各自一个 Grok routine 的 webhook 适配器
   dev-server（开发服务器）  agents: codex（default）                     exec 适配器 → Codex app-server thread/queue/add

   grok 发：  tailmsg send dev-server/codex "今天的书签简报" --file report.json   # from_agent=grok
   codex 回： tailmsg reply <id> "已入队"                                          # 自动 agent=grok，只叫醒 grok
   发给私信联系人： tailmsg send bot-box/私信联系人 "帮我约周五" --wake now
   ```

### 兼容性

`wake` 是 v1 里新增的**可选**字段，`v` 仍是 `1`，没带 `wake` 的旧发件方照常工作。但**旧版收件方会把含 `wake` 的信当成未知字段返回 400**。所以：
- `tailmsg send` / `reply` 只有显式加了 `--wake` 才会写这个字段，不加就不写（效果等于 `batch`）；
- 确认对方已升级之前，不要给它发 `--wake`；被 400 时去掉 `--wake` 重发即可。

`agent` / `from_agent` 同理，也是 v1 的可选字段，`v` 仍是 `1`：
- **对方升级到支持 @agent 的版本之前，不要点名 agent**（`机器/agent` 或 `--agent`），旧版会 400。显式点名的信被 400 时 tailmsg 不会偷偷去掉 `agent` 重发，免得叫错人。
- `from_agent` 只在配了 `self_agent` 或加了 `--as` 时才写。由 `self_agent` 自动带上的 `from_agent` 如果被旧版收件方以未知字段拒收，`send` 会去掉它、用同一个 `id` 重发一次（第一次没被存下），并在 stderr 提示；`--as` 显式指定的不会被去掉。
- 没写 `agent` 的旧信，新版收件方照常接收，按默认 agent 处理。

## 6. 配置 `~/.tailmsg/config.json`

```json
{
  "port": 7766,
  "access_policy": "tailnet",
  "allow": [],
  "deny": [],
  "tailscale_cmd": ["tailscale"],
  "bind": null,
  "wake_adapter": null,
  "hook_timeout_s": 120,
  "wake_attempts": 3,
  "wake_backoff_s": 2,
  "outbox_ttl_hours": 24,
  "max_msg_bytes": 262144,
  "max_fetch_bytes": 1073741824,
  "wake_policy": {"default_max": "batch", "peers": {"dev-server": "now", "bot-box": "now"}},
  "quiet_hours": {"start": "23:00", "end": "08:00", "tz": "Asia/Tokyo"},
  "wake_rate": 6,
  "self_agent": "grok",
  "default_agent": "grok",
  "agents": {
    "grok": {"wake_adapter": {"type": "webhook", "url_file": "~/.secrets/tailmsg-wake-url-grok",
                              "secret_file": "~/.secrets/tailmsg-wake-secret-grok"}},
    "私信联系人": {"wake_adapter": {"type": "webhook", "url_file": "~/.secrets/tailmsg-wake-url-dm",
                                 "secret_file": "~/.secrets/tailmsg-wake-secret-dm"}, "wake_max": "now"}
  }
}
```

- `access_policy`：`tailnet`（新配置默认）允许通过 `tailscale whois` 验证且网络允许接入的节点；`allowlist` 还要求命中 `allow`。旧配置含 `allow` 但未声明该字段时保留原来的白名单行为，升级不自动扩权。无效策略或规则按拒绝处理。
- `allow` / `deny` 支持短主机名或 FQDN、`tag:xxx`、`user:登录邮箱`。`deny` 在两种模式下都优先于允许规则。`allowlist` 下空 `allow` 不接收对端消息；`tailnet` 下不要求填写 `allow`。
- `yobiru policy show` 查看有效策略；`policy set --mode tailnet|allowlist` 预览，`--apply` 才写入并备份。`--allow` / `--deny` 替换整个对应列表，未指定则保留；`--clear-allow` / `--clear-deny` 清空。0.4 接收端和桥接在下一次权限检查时读取本地策略，不需为单独的策略修改重启。策略更新不改变身份、已有数据、agent 路由、会话、适配器、唤醒限制或回复权限。
- `tailscale_cmd`：如果 CLI 需要 root，改成 `["sudo","-n","tailscale"]`；macOS 的 App 版改成 `["/Applications/Tailscale.app/Contents/MacOS/Tailscale"]`。
- `wake_adapter`：唤醒适配器，见 §5.8。`null` 表示不叫醒，`now` 的信也只是存起来。旧字段 `on_message: [...]` 等同 `{"type":"exec","argv":[...]}`，两者都写时以 `wake_adapter` 为准。
- `hook_timeout_s` / `wake_attempts` / `wake_backoff_s`：适配器单次超时、总尝试次数、退避基数（秒）。
- `wake_policy`：每个发件方最多能把你叫到哪一级（`now` / `batch` / `none`）。不写就是谁都最多 `batch`，即默认不会叫醒。
- `quiet_hours`：夜间静音窗口，可以跨午夜；`tz` 用 IANA 时区名，不写用本机时区；`null` 表示不静音。
- `wake_rate`：每个（发件方, agent）每小时最多几次实际 `now`。
- `self_agent`：本机发信时自己的 agent 名，作为 `from_agent` 发出去，方便对方 `reply` 点名回来。不写就不带。
- `default_agent`：没点名的信归谁。它可以不在 `agents` 里；没在 `agents` 里配适配器时用顶层 `wake_adapter`。
- `agents`：`名字 → {"wake_adapter": {...}, "wake_max": "now|batch|none"}`，两项都可省略（`{}` 表示这个 agent 存在、能收信，但暂时不叫醒）。适配器格式同 §5.8。名字不合法时 tailmsg 拒绝启动。
- 通信本身不需要任何密钥，身份全部来自 Tailscale。唯一可能用到的密钥是 webhook 适配器访问本地 agent 的凭证，只放在环境变量或 0600 文件里。

## 7. 网络层要求（tailnet 策略）

- 收件机器上 Tailscale 不能开 shields-up（`tailscale set --shields-up=false`），否则别的机器连不进来。
- 建议在 ACL / grants 里只放行 `tcp:7766`，并且只允许互相通信的这几台机器访问。Tailscale 的 ACL / grants 始终生效；Yobiru 的本地访问策略不能绕过它们。使用 `tailnet` 模式时无需再逐台填写 Yobiru 白名单，仍可用 `deny` 拒绝指定节点、tag 或用户。

## 9. 发现接口 `GET /info`（Yobiru 0.3.0 起，可选）

鉴权顺序与 `/msg` 相同，并且在读取任何元数据、查询任何会话名称**之前**完成：来源必须是 tailnet 地址，`tailscale whois` 查出的身份必须符合本机访问策略（本机诊断保留本机身份例外），否则 403；拒绝规则优先。身份查询暂不可用时与 `/msg`、`/f/...`、`/requests/...` 一样返回 §5 所述的 503。

```json
{"ok": true, "v": 1, "product": "yobiru", "version": "0.5.0", "node": "dev-server.example.ts.net",
 "capabilities": ["request-lifecycle-v1"],
 "agents_total": 2, "agents_truncated": false, "generated_at": "2026-10-11T00:00:00Z",
 "agents": [
  {"agent": "codex", "default": true, "session": {
     "runtime": "codex", "id": "<会话 UUID>", "id_source": "codex-bridge",
     "display_name": "入口", "confirmed_title": null, "confirmed_at": null,
     "native_name": "Triage inbox", "native_name_status": "ok", "native_name_checked_at": "2026-10-11T00:00:00Z",
     "native_name_cached": true, "native_name_stale": false, "activity": "unknown"}},
  {"agent": "grok", "default": false, "session": null}]}
```

- `version` 是 Yobiru 产品版本，不是协议版本（协议仍是 v1）。最多列出 32 个 agent，超出时 `agents_truncated` 为 true。
- 只投影**明确绑定**的会话：`<home>/sessions.json`（`yobiru session bind` 写入）中的绑定，以及已安装 Codex 桥接的固定入口会话（agent `codex`，以桥接配置为准，冲突的绑定不会改变它）。绝不返回配置、路径、适配器 argv、webhook 地址、密钥、消息预览、会话记录、任意会话列表或账户信息。
- `display_name` 是本地别名，`confirmed_title` 是用户绑定时确认的标题；两者都不是原生名称。`native_name` 只来自只读查询：Codex 桥接用 `thread/read`（`includeTurns: false`）读取绑定 UUID 的 `Thread.name`，从不使用 `preview`（首条用户消息），不启动模型回合。查询有界（子进程组超时清理、输出上限），结果按进程缓存约 5 分钟（失败 1 分钟）；冷查询超过约 1.5 秒时返回 `pending`，或返回带 `stale` 标记的旧值。短时读取器看到的 `notLoaded` 等状态不代表会话所属运行时的状态，因此 `activity` 永远是 `unknown`。
- 发现方必须把整个响应当作不可信数据：限制大小与总时长，校验类型，去掉控制字符和终端转义，Codex 会话 ID 必须是小写 UUID。

### 升级通知（kind `yobiru-upgrade-notice`）

普通的 v1 消息，请求 `wake: "now"`（接收端策略照常降级），`text` 是 JSON：`{"type":"yobiru-upgrade-notice","product":"yobiru","version":"0.4.0","from_node":...,"source":"https://github.com/chekusu/yobiru","release":<已核实的 release 页面>,"tag":...,"note":...}`。它只是信息，不是命令、批准或脚本；收件方不得据此执行任何内容，也不得转发。发件方（`yobiru update notify`）只在本机插件、常驻运行时和运行中的接收端都已是已发布的官方版本后发送，只发给已知安装，并在本地 `upgrade-notices.json` 中按（节点、端口、agent、版本）去重：先持久记录意图和固定的信封 ID 再 POST；只有 202/200 且 `ok=true`、`id` 与信封一致才算 `notified`；发出后超时或回执无效记为 `uncertain`，绝不自动重发（显式核对时用完全相同的 ID 重发，接收端按 ID 去重）。403 记为 `blocked`，只有对方 `/info` 再次通过鉴权（即访问策略已允许本机）或显式 `--retry-blocked` 时才再发。

### 升级通知的离线恢复（0.4.0）

`update maintain` 默认为预览；`--apply` 才记录更新检查并恢复**已有通知账本中的收件方**。它不发现新收件方、不为新版本自动创建通知、不安装软件、不修改任何远端策略。成功获取的官方 release 元数据缓存六小时。只有核实官方版本且本机插件、运行时、运行中接收端版本一致并不早于目标版本，才允许重试发送；接收回执与运行版本核验仍是不同状态。`uncertain` 不自动重发，需显式 `update notify --reconcile-uncertain`；`blocked` 仍须鉴权恢复，`rejected` 不重试。

`update reminder-template systemd-user|launchd|cron --retry-notices` 打印每 15 分钟及启动后运行 `update maintain --apply` 的可选任务定义（systemd 包含 `Persistent=true`），由用户或已获授权的 agent 安装。插件升级后路径可能变化，应重新生成。没启动接收端、仍然离线或策略阻挡的旧机器不能被远端强制升级。恢复仅覆盖升级通知，普通消息没有离线自动重投承诺。

## 10. 请求生命周期（Yobiru 0.5.0 起，可选）

请求和通知有不同的约定：**请求必须留下回执和最终响应；通知不要求回复。**
最终响应可以是完成、失败、拒绝或权限/环境阻塞，不等于必须执行请求里的任务。
消息和附件仍不是授权；处理者只使用已有权限。接收端断电、agent 从未运行或网络不可达时，
协议不能制造实际结果，发送方会看到尚未返回结果或超时，而不是虚假的完成。

### 能力协商与兼容

`GET /info` 的 `capabilities` 包含 `request-lifecycle-v1` 才表示支持此机制。
`yobiru request send PEER TEXT --timeout-seconds 900` 和 `send --kind request` 默认创建跟踪请求，
在发送前验证对端能力；旧端或无法确认能力时不悄悄退化成普通请求。
明确接受没有回执、状态查询和最终结果约束时，才使用 `send --kind request --legacy-request`。
旧 v1 信封仍可接收，历史 `kind=request` 本身没有生命周期保证；判断依据是 `request` 元数据。
普通 `text`、`notice`、`yobiru-upgrade-notice` 不带请求元数据，也不自动产生状态响应。

### 信封扩展与状态接口

请求信封使用 `kind: "request"`，并增加：

```json
{"request": {"timeout_seconds": 900, "reply_port": 7766}}
```

`timeout_seconds` 为 1–604800 的整数；`reply_port` 为 1–65535 的整数，指定发件机器接收响应的端口。
请求不能同时带 `response` 或 `reply_to`。接收端仍只向经过认证的原发件机器回复，
不会把端口参数解释为其他主机或 URL。

响应是一个独立、具有固定 `id` 的信封，使用 `reply_to` 指向原请求，并增加：

```json
{"kind": "request-result", "reply_to": "9f2c4e1ab07d33c5", "wake": "none",
 "response": {"state": "completed", "seq": 3}, "text": "结果及证据……"}
```

`seq` 为从 1 开始递增的整数。`received` / `running` 使用 `kind: "request-receipt"`；
`completed` / `failed` / `rejected` / `blocked` / `timed_out` 使用 `kind: "request-result"`。
响应必须有 `wake: "none"`，状态、关联 ID 和认证对端都必须匹配，旧响应不能覆盖更新的结果。
这两个保留 kind 不接受缺少 `response` 元数据的信封。

`GET /requests/ID` 对有效请求返回 `{"ok": true, "request": {...}}`，
先验证 Tailscale 身份及访问策略，再确认调用者就是原请求的认证发送机器；
其他已认证机器或不存在的 ID 返回 404。返回状态包含 `state`、`last_state`、`deadline`、
`overdue`、`outcome_known`、`timed_out_at`、`late`、`seq`、`submission` 和最近的 `response`。
它不提供任意机器读取整个请求账本的接口。

发送方验证响应与原请求的认证身份、agent 和 ID 匹配后，将响应保存在本机账本的
`response_events` 中，正常接收流程不创建普通 inbox JSON，也不调用唤醒适配器。
从 `request status REQUEST_ID --json` 取得 `response.id` 后，可用 `show RESPONSE_ID`
查看完整响应、用 `fetch RESPONSE_ID` 获取其附件；两者可以从已认证账本读取该响应，
无需先将它放入普通收件箱。下载文件保存到 `<home>/inbox/RESPONSE_ID/files/`，
仍检查原认证发送方、大小及 SHA-256。不要给响应再回复、ack 或手动唤醒它。

### 收到、处理中、最终响应

1. 接收端通过正常 Tailscale 身份和访问策略验证，持久保存请求后，自动记录最小 `received` 回执。
   回执不复制消息正文，不表示 agent 已阅读、接手或有权执行。自动协议响应使用 `wake: none`，
   不再触发回复，避免循环。
2. Agent 读取并核对授权，开始处理前执行：

   ```sh
   yobiru request respond REQUEST_ID "已接手，正在检查。" --state running
   ```

3. 处理结束必须持久保存一个最终响应，包含实际结果或明确原因：

   ```sh
   yobiru request respond REQUEST_ID "检查完成：结果及证据……" --state completed
   # 或 --state failed / rejected / blocked
   yobiru ack REQUEST_ID
   ```

   `completed` 是处理者报告完成；`failed` 是执行失败；`rejected` 是决定不处理；
   `blocked` 是当前缺少权限或必要条件。`blocked` 结束这次请求的等待，并不授予缺少的权限。
   `reply REQUEST_ID TEXT` 对跟踪请求默认按 `completed` 保存最终响应；进度必须用 `running`，
   不能把“已经入队”当完成。`request respond` 和 `reply` 都支持 `--file PATH` 附加结果文件，
   可配 `--ttl-hours`；跟踪回复始终沿原认证身份路由，不允许 `--agent` / `--as` / `--port` 改投。`ack` 在最终响应或明确表示结果未知的 `timed_out` 未持久保存时拒绝执行；
   超时绝不能记为完成。
4. 回执和响应进入接收端的持久响应发件队列，重试复用同一个响应 ID；重启后可继续发送。
   已持久保存不等于已送达。发送方或接收端离线期间不能保证即时回执；恢复后可重投响应，
   或由发送方主动查询。

### 超时与状态查询

```sh
yobiru request status REQUEST_ID --json            # 本机保存的状态
yobiru request status REQUEST_ID --refresh --json  # 向原接收端核对
yobiru request list --json
yobiru request status REQUEST_ID --direction incoming --json  # 收件端本机状态
yobiru request maintain                                      # 单次维护和响应重试
```

请求、状态和响应队列持久保存在数据目录下 `requests/ledger.sqlite3`。
运行中的 `serve` 自动维护期限并重试待发响应；`request maintain` 可手动运行一次。
本地 `status` / `list` 只读并显示计算得到的超时状态；只有 outgoing 查询支持 `--refresh`，
后者会联系原接收端并保存核对得到的响应。
发送方在投递之前保存请求 ID 和期限，发送过程超时仍可按该 ID 核对。
超过期限还没有最终响应时，状态显示 `timed_out`：**实际执行结果未知**，不是失败、取消、
或允许安全重做。不要直接再发一份新请求；先刷新状态、核对接收端和实际业务效果。
晚到的有效最终响应可以更新结果，并标明迟到。状态、响应必须与原请求及其认证对端匹配；
其他设备不能替它报告完成。

### 本机生命周期证据与维护（0.6.0 起）

这些是接收端本机账本和诊断的扩展，不改变线上协议或信封格式。

- 账本以追加列的方式记录本机证据（`NULL` 表示未知，旧版运行时仍可使用同一账本）：`received_at`（持久接收）、
  `started_at` / `progress_at`（owner 第一次和最近一次报告 `running`）、`final_at` 与 `final_response_id`（持久最终结果
  及其确切响应）。它们与状态和待发送响应在同一事务中写入，时间取该响应自身的时间戳。迁移只回填接收端自己写下的证据，
  并按响应序号而不是时间戳排序，时钟回拨不会颠倒先后。只有 owner 报告的 `running`（或其后的结果）能证明工作已开始；
  回执、入队或会话中出现的通知标记都不能。
- `request status` 增加 `start_confirmed`、`timeout_persisted` 等字段：`state` 仍是计算视图（过期请求在维护持久化之前也
  显示 `timed_out`），`timeout_persisted` 说明超时是否已持久记录。最终结果是否送达，只看该最终响应本身的
  `delivered_at`，从不看更早的回执。
- 运行中的 `serve` 用三个受监督的维护线程分别处理请求生命周期（inbox 恢复、到期）、响应投递和附件清理。服务循环只在
  线程确实结束后（`is_alive()` 为假）启动一个替代线程，并有退避；运行缓慢的线程绝不会被重启或复制。每轮的各阶段相互
  独立，只有全部成功才算一次完整成功。线程把只含元数据的心跳写入 `<home>/maintenance.json`（0600，原子替换并
  fsync），新进程使用新的 instance，不会把旧进程的心跳当作自己仍在运行。
- 响应投递在同一数据目录内跨进程互斥（POSIX 用 `flock`，Windows 用 `msvcrt`），另一个调用者得到 `busy` 且不发送。每轮
  按目的地轮转，一个目的地失败一次后本轮即跳过它；每轮有尝试次数和时间上限，每次交换有总时限，超时、截断或长度
  不符的应答都视为未确认，继续用同一响应 ID 重试。
- `GET /health` 对任何 tailnet 地址仍返回 200（传输可达），并增加 `receiver_alive`、`status`（`healthy` / `starting` /
  `degraded`）和 `maintenance`（各组件状态和固定原因代码）。维护受损时 `ok` 为 `false`。计数、时间和积压只在本机
  （`maintenance.json`、`doctor`）提供，不通过 `/health` 暴露。`request maintain` 输出各阶段结果，失败时退出码为 1。

### 权限、通知和边界

- `allow_replies` 控制 Codex 入口发送业务回复；升级不自动开启，也不能由对端正文改变。
  `false` 时不得自动发送模型生成的结果或敏感内容，不得改设置来完成请求。
  Codex 桥接记录固定的最小 `blocked` 结果并跳过模型执行。
  接收服务的最小协议回执不包含用户内容，也不代表自动业务回复已获授权。
- 通知处理后本地 ack 即可，不给通知或回执再发“收到”。需要升级验收结果时，
  在升级通知之外另发一个经授权的跟踪请求；升级通知本身不要求回复。
- 这是持久的通信状态与结果约定，不是任务调度器，也不提供任务恰好执行一次、
  业务副作用的自动回滚，或离线设备必定完成任务的保证。接收回执也不是已读回执。

## 11. 单向共享请求（可选 `request-pull-v1`）

双方先通过已鉴权的 `/info` 协商 `request-lifecycle-v1` 和 `request-pull-v1`。
CLI 显式使用 `request send ... --transport pull`，缺省仍为既有 push；不支持时拒绝，不静默降级。
该模式中全部 TCP 连接由请求方发起，接收方不向请求方建立反向连接，请求方无需运行 receiver。
部署与验证步骤见 [SHARING.md](docs/SHARING.md)。

### 稳定身份

Tailscale 共享设备的 `Node.Name` 可能为空。接收端从 LocalAPI whois 验证源 IP 在 `Node.Addresses`
单地址前缀内、`Node.ID` 和 `Node.User` 有效、`UserProfile.ID` 匹配，再使用 `Node.StableID`。
规范标识为 `node:` 加 StableID 的 ASCII 字节小写十六进制编码，保留原 StableID 的大小写区别。
pull 信封 `from` 必须为该标识；不信任信封自报名称、ComputedName 或仅用于展示的主机名。
访问与 wake 规则可使用 `node:<hex>` 精确匹配；既有 DNS、tag、user 规则保持原有语义。
接收端所有受保护操作仍检查当前访问策略；客户端记录接收方稳定身份，轮询及下载前核对。

### 上传与提交

请求元数据保留 `timeout_seconds`、`reply_port`（兼容字段，pull 不用于反连），增加：

```json
{
  "transport": "pull",
  "uploads": [{
    "token": "<32到64位URL安全随机token>",
    "name": "input.bin",
    "sha256": "<64位小写hex>",
    "size": 2048,
    "mime": "application/octet-stream"
  }]
}
```

1. 请求方先复制不可变文件快照，保存请求 ID 与完整信封；不使用原有 `fetch` 字段。
2. `POST /requests/prepare` 携带完整 JSON 信封，持久预留相同 ID、认证请求方、不可变 manifest。
   返回 201（新预留）或 200（完全相同的重试），异主或异内容同 ID 冲突拒绝。
3. 对每份文件 `PUT /requests/<id>/uploads/<token>`，要求一个准确的 `Content-Length`，不接受分块编码。
   接收端在截止时间内检查请求方、大小与 SHA-256，持久写盘后返回 201/200 和文件校验摘要。
4. 最后 `POST /msg` 提交原始信封。全部附件校验并就绪到 `inbox/<id>/files/` 后才入账、返回 202，
   按既有 wake 权限处理。同 ID 已提交的重试返回 200，不重复执行。过期请求不唤醒 agent。

每请求最多 100 份文件，总大小不超过接收端 `max_fetch_bytes`。路径经规范化并禁止重名、重复 token、
路径穿越与无效 hash。上传中断只留下可重试的预留；`request resume ID` 使用原 ID、manifest 和快照继续。
截止、超时或网络失败都不能作为自动创建新请求的依据。

请求方在预留前、每次上传连接前（校验完快照之后）和最终提交前，都重新确认该地址仍属于保存请求时固定的
Tailscale 稳定身份、本机策略仍允许它，且本机仍是发送方；任一项变化即停止，不向替换后的对象发送，
请求保持同一 ID、状态未知，可以 `request resume`。接收端在上传后的身份复核暂时无法查询时返回 §5 的 503
`identity_unavailable` 并丢弃该次上传。请求方快照、接收端上传与入站附件、结果附件和下载文件都以 0600 创建，
新建的目录为 0700，与进程 umask 无关；已有目录的权限不变。

### 处理与返回

`fetch REQUEST_ID` 对 pull 请求只校验本地已上传文件；不访问请求方。
接收 agent 继续使用 `request respond` / `reply`，结果与状态持久写入请求账本，**不加入反向发送队列**。
返回 `stored: true, transport: "pull"` 代表结果已存储，不代表对方已领取。
同一最终结果的重试（状态、文本相同，附件的名称、大小和 SHA-256 依次相同）返回已保存的结果和原有下载地址；不同的最终结果被拒绝。被拒绝或失败的尝试只删除它自己新暂存、且账本中没有任何响应引用的副本；无法读取账本时保留副本。

请求方使用 `GET /requests/<id>`（CLI `request status ID --refresh`）领取自己的状态与最新响应。
访问依赖当前授权和原稳定身份，不可查询他人请求。响应仍是 §10 的关联 ID、递增序号、状态和正文。
响应附件由接收端 `/f/<token>/<name>` 提供，只允许原请求方，且重新检查当前 allow/deny。
请求方 `fetch RESPONSE_ID` 核对接收方身份、文件大小与 SHA-256，保存验证后的文件。

pull 上传及结果附件保留到显式本地清理，禁止 `--ttl-hours`；已接受请求和结果在请求截止后仍可领取。
本模式不赋予接收方主动发起新会话的网络权限，也不扩大本地任务执行、回复或 wake 权限。

## 12. 公开 bot 联系人与 Friends（0.6.0 起，独立协议）

公开 bot 联系人**不是** tailmsg v1 的一部分，也不改变本规范的任何行为。它是一套独立的 HTTP API
（`/v1/...`），由所有者自行托管的网关进程提供（默认只监听 `127.0.0.1:7780`，对外须经 HTTPS 反向代理）。
该网关不接收 `POST /msg`、`/info`、`/health`、`/requests/` 或 `/f/` 请求，不访问 7766 端口的接收端，不调用
`tailscale`，不运行唤醒适配器，也从不把公开内容写入 tailmsg 收件箱或 Codex 桥接队列。

身份来自网关签发的凭证（所有者、匿名来访者、按 bot 划分的 worker、按 bot 划分的能力校验方），而不是
Tailscale 身份或信封中的 `from`／`from_agent`。来访者与所有者的设备都不需要加入同一个 tailnet。协议、
信任边界、状态、限制、存储与部署见 [docs/PUBLIC_BOTS.md](docs/PUBLIC_BOTS.md)。

## 8. 刻意不做的事

私有消息不做中心服务器、共享任务调度器、已读检测或任务恰好一次执行；不替接收 agent 授权。
（可选的公开 bot 网关是所有者自行托管的消息存储，规则见第 11 节。）
普通消息没有自动重投保证（唤醒适配器有限重试，升级通知有维护恢复，0.5 请求响应有持久重试）。
内容传输加密与机器身份由 Tailscale 提供；Yobiru 不另加静态存储加密。
