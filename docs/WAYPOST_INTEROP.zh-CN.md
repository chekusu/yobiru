# Yobiru 直接投递，Waypost 留存交接

**状态：可选择启用的文本桥接。** 不修改双方协议，不改变 Yobiru 的默认投递路径。本版不转附件，也不自动同步远端任务的处理结果。

[English](WAYPOST_INTEROP.md)

Yobiru 负责机器之间直接送信、唤醒和本机策略。[Waypost](https://github.com/mainline-org/waypost) 负责持久信箱和有版本的原始资料。需要离线后继续读报告，或让主助手收齐多位助手的结果时，可以把两者接起来。它们各自也能独立使用。

| 你的需要 | 使用 |
|---|---|
| 直接送达、唤醒、安静时段、机器访问策略 | Yobiru |
| 报告留存、离线阅读、主助手汇总 | Waypost |
| 从对方机器拉取文件 | Yobiru `fetch` |
| 引用某一固定版本的资料 | 单独授权 Waypost Blob 与资料版本 |

桥运行在你信任的机器上，会读取被转发的明文。它使用普通的 Waypost 助手连接，不需要个人登录密钥或服务管理密钥。

## 能做什么

- 将本机已经收到的 Yobiru 文本，投递给配置中的 Waypost 收件人。
- 将指定 Waypost 发信人的消息，以 `notice`、`wake: none` 送往配置中的 Yobiru 机器和助手。
- 先在本地 SQLite 保存正文与转发状态，再执行发送或确认。重启后保留进度，沿用原来的 Waypost 请求与回执 ID。
- 来源认证缺失、没有匹配路由、包含附件，或已有账本的空间/身份/路由发生变化时，明确失败，不把消息当作成功处理。
- 日志只报告 ID、数量和状态，不输出私人正文或凭据。

这是转发器。收到的文字仍然是请求，不构成执行批准；送达通知也不证明远端助手已阅读或完成工作。

## 为一个空间配置一座桥

本版桥运行于 macOS/Linux（POSIX），需要 Python 3.8+ 和现有 Yobiru 启动器；尚未支持 Windows 的文件锁与权限检查。为桥单独接入一个 Waypost 助手，授予 `message.send`、`message.read`、`receipt.write`；发信范围只包括入站路由中的收件人 Actor ID。它只读自己的 Consumer，不读其他助手的信箱。

把这份助手凭据放进私密文件，POSIX 权限设为 `0600`。Yobiru 数据目录和桥的账本也应私密保存。不要填服务管理密钥。

建立 `routes.json`，将示例 Actor ID 换成 Waypost 中的真实 ID：

```json
{
  "incoming": {
    "alpha.example.ts.net/grok": "actor_primary"
  },
  "outgoing": {
    "actor_primary": "alpha:7766/grok"
  }
}
```

`incoming` 使用**经过认证的完整机器名**和发件信封的 `from_agent`。发件方需要配置 `self_agent`，或在发信时使用 `--as`。`outgoing` 的键是允许转出的 Waypost 发信人 Actor ID，值是固定的 Yobiru 地址。消息正文不能另选目标。

`alpha/grok` 只是显示和路由名称，不能代替 Waypost Actor ID。Yobiru 短机器名仍按当前节点状态解析，重名、离线或访问被拒绝时不会偷偷改投其他机器。

先查看计划，再实际转发：

```sh
python3 tools/waypost_bridge.py \
  --yobiru /path/to/yobiru/scripts/yobiru \
  --home /path/to/private/yobiru-home \
  --waypost-url https://your-waypost.example \
  --token-file /path/to/private/bridge-token \
  --map /path/to/private/routes.json \
  --state /path/to/private/bridge.sqlite3 \
  --once --dry-run
```

移除 `--dry-run` 才会发送；移除 `--once` 后持续轮询。预览可以认证、读取信箱，但不发信、不确认、不创建账本。重启时保留账本；账本损坏或不兼容是错误，不等于可以从头重发。

远端 Waypost 必须使用 HTTPS，明文 HTTP 只允许本机测试。客户端拒绝重定向，避免把 bearer 送到另一个服务。

## 身份以接收端记录为准

Yobiru 保存的结构是 `{"envelope": ..., "received": {"peer": {"node": ...}}}`。桥只信 `received.peer.node` 中接收端认证过的来源。缺少认证记录或信封来源相矛盾时拒绝处理，不能回退到 `envelope.from`。

`from_agent` 只在这台已认证机器内分路。同一机器权限下的其他进程也能自报这个名字，因此助手名不构成租户隔离或身份认证。不要据此混放彼此独立的用户。

在 Waypost 里，发件人始终是桥凭据绑定的 Actor；原始 Yobiru 来源写入正文，供核对。桥不会按收到的名字创建身份，也不冒充其他 Waypost Actor。

## 重启、丢响应与回执

**Yobiru → Waypost：** 请求 ID 由空间、认证机器、助手名和原信 ID 派生，正文先存本地账本。Waypost 已提交而响应丢失时，重试仍用同一个请求 ID 和正文，不会多发一封。

**Waypost → Yobiru：** 从 feed 的 `resource_id` 读取真实正文，持久保存 feed 项和消息后写 `received`。只有 Yobiru 明确返回 HTTP 200/202、`ok: true` 且回信 ID 匹配，才写 `processed`。回执响应丢失只重试回执，不再送一遍信。

这里的 `processed` 表示**这座桥已完成转发**，不表示远端助手完成任务。本版不自动同步 `running`、`completed`、`failed`、`rejected`、`blocked`、`timed_out`、`late` 等任务状态，尤其不会把超时当成功。

Yobiru 普通 `send` 每次调用都会生成新 ID。因此，发送时崩溃、超时或丢响应，桥将其记为 **unknown（结果未知）**，阻止自动重发，也不写 `processed`。需要运行者核对接收机器的信箱和真实效果，再处置未决项。不要为了清除错误删除账本或重新发起一次发送。这个机制防止盲目重复，不承诺远端任务恰好执行一次。

核对接收端确实已有这条消息后，用同一组连接参数追加 `--confirm-sent ITEM_ID YOBIRU_ID`：只补记回执，不重复发送。只有确认接收端从未收到、也没有相关效果时，才可追加 `--retry-unsent ITEM_ID`，授权下次运行重试。两种操作都不会自动处理其他消息。

账本绑定 Waypost 空间、Actor、Consumer 和路由配置。不同桥使用不同私密账本；同一账本不能被两个进程同时用于发送。

## 关联信息放正文里

Yobiru 会拒绝未知信封字段，因此跨服务 ID 只放在 JSON 文本正文中，不增加顶层协议字段。入站保留 Yobiru 原信 ID 和回复关联，出站保留 Waypost 消息身份。它们用于核对记录，不赋予改投地址或执行任务的权限。

## 限制

带 Yobiru `fetch` 或 Waypost 附件的消息整条拒绝，不遗漏文件后再报成功。附件上传、校验和版本资料需要额外授权与独立传输实现。

本版不安装后台服务，不提供托管中继、群聊、远端任务批准、自动主助手配置或多用户路由。大小上限和隐私过滤以实际连接的 Waypost 为准；超限拒绝，不截断正文。

## 如何验证

```sh
python3 -m unittest discover -s tests -p 'test_waypost_bridge*.py' -v
python3 tools/build_plugin.py check
```

先在 [Waypost](https://github.com/mainline-org/waypost) 执行 `cargo build --locked`，再提供本地二进制运行真实互通验收：

```sh
WAYPOST_TEST_BINARY=/path/to/waypost/target/debug/waypost \
  python3 -m unittest discover -s tests -p test_waypost_bridge_e2e.py -v
```

2026-10-16 已通过 22 项桥接测试（14 项映射与安全测试、8 项集成场景）和打包检查。完整 `tests/` 最终运行 250 项，其中 35 项跳过、四项失败，均已在未修改的 PR 基线 `1265921` 复现：两项依赖 `/bin/false`，一项比较 `/var` 与 `/private/var`，一项要求连接拒绝而本机实际返回超时。这些既有环境差异与桥接结果分开记录。

测试启动全新的加密 Waypost 和两台真实 Yobiru HTTP 接收端，并使用真实打包后的 CLI。仅 Tailscale 身份查询和本机网络范围使用 Yobiru 已有测试替身。覆盖双向文本、重启、回执、两侧丢响应、缺少认证和附件拒绝；不连接真实 tailnet、生产数据或外部模型。

接口依据固定版本的 [Waypost 路由](https://github.com/mainline-org/waypost/blob/926601dce1b5b4bad218c52c75880e6eb48d158d/src/http.rs)、[身份响应](https://github.com/mainline-org/waypost/blob/926601dce1b5b4bad218c52c75880e6eb48d158d/src/service/mod.rs) 与[用户指南](https://github.com/mainline-org/waypost/blob/926601dce1b5b4bad218c52c75880e6eb48d158d/docs/user-guide.md)，Yobiru 身份与信封规则仍以 [SPEC](../SPEC.md) 为准。
