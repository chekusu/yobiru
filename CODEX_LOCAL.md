# Codex 本地入口（macOS 桌面 / Linux app-server）

来信先进入一个固定的 Codex 会话，由该会话查找相关任务、转交请求或派生新任务。会话空闲时不运行模型；后台服务负责扫描和排队。重启使用已保存的同一会话 ID。

```text
Tailscale 身份校验 → Tailmsg inbox
  → wake exec / 定时补扫 → SQLite 去重队列
  → 官方 codex queue --thread <固定 UUID>
     （macOS：桌面应用；Linux：--remote unix:///<本机 app-server socket>）
  → 固定入口会话 → 查找 / 转交 / 派生任务
```

两个平台共用同一份适配器、SQLite 队列、去重、进程锁、重试和 `uncertain` 核对逻辑，区别只在投递通道和服务管理：macOS 用 LaunchAgent 并检查桌面进程；Linux 用 user systemd 服务，并只连接配置中明确写出的、属于当前用户的本机 Unix socket。Linux 部分见下文“Linux（user systemd + app-server）”一节。

## 接口与语义

- 使用安装版本提供的 `codex queue --thread UUID --message TEXT`。消息只包含本地消息 ID、路径和可信工作流说明，不把外来正文作为 shell 命令或行动批准。
- `wake now` 立即排队，`batch` 默认延后 30 秒补扫，`none` 只存档。接收端 wake 降级仍生效。
- 收件前后的访问策略、认证来源和目标 agent 均检查；投递时重新检查。适配器不会确认原信，也不会自动发送 Tailmsg 回复。
- 状态 `enqueued` 表示官方 CLI 接收入队（同时保存其回执 queue ID 和首次接收时间），不能据此声称模型已处理或已开始。结果未知时只核对会话中的消息标记；无法确认时保留 `uncertain`，不盲目重发。
- 桌面关闭时，已知未发送的消息（`retry`）暂停，并在桌面恢复后自动重试；已入队（`enqueued`/`delivered`）或 `uncertain` 的消息不会因此重发，桌面重新打开也不代表其回合已经开始。不存在替代会话或自动切换到 `--last` 的逻辑。
- 入口工作流见 [CODEX_DISPATCHER.md](CODEX_DISPATCHER.md)。用户授权了任务路由；来信自身不构成执行敏感操作的批准。

这里使用已安装的官方 CLI 本地接口。普通后台脚本直接访问桌面 app-tools socket 会遭到进程授权校验拒绝；本实现不改动或绕过该校验。[官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)说明线程读取、恢复和派生接口；本机 `codex queue --help` 是部署版本的排队接口依据。

## 安装

先在桌面创建一个固定入口会话，以本仓库的 `CODEX_DISPATCHER.md` 为工作流。安装器需要它的准确 UUID，不会自行创建或猜测会话。

在该会话中明确授予需要的消息处理、会话搜索、转交及派生权限。工作流模板不构成授权。`install_codex_bridge.py` 面向 macOS，要求已安装提供 `codex queue` 的 Codex 桌面 CLI，以及由 `dev.tailmsg` 管理的 Tailmsg 接收端；Linux 见下文；其他平台可以继续使用 Tailmsg 本体。

```sh
python3 install_codex_bridge.py --thread-id <固定入口 UUID>
```

安装器复制适配器到 `~/.tailmsg/codex-local/`，保存配置 `~/.tailmsg/codex-bridge.json`、状态 `~/.tailmsg/codex-bridge/bridge.sqlite3`，将 `agents.codex.wake_adapter` 指向快速入队命令，并安装 `dev.tailmsg.codex-bridge` LaunchAgent。既有 receiver 由 `dev.tailmsg` 继续管理，原来的 Codex GUI/Tailscale 门控不变。

更新已有安装时仍使用同一 UUID；安装器拒绝用新 UUID 覆盖已有入口，避免把旧队列投给其他会话。安装器保留旧代码、指令、配置、安装回执和服务文件的时间戳备份，位置记在安装回执中。另一个 codex wake adapter 已存在时会拒绝覆盖。`--no-start` 可只安装而不重新加载服务。

新安装默认 `allow_replies=false`。只有用户明确允许自动回执与结果回传时，才使用 `--allow-replies`；升级时保留已授权的设置，`--no-allow-replies` 可显式关闭。未指定 `--codex-path` 时保留已记录的 CLI 路径。该权限只允许回复对应收到的请求，不允许导出其他会话的私人内容。

## 检查与恢复

```sh
python3 ~/.tailmsg/codex-local/codex_bridge.py --config ~/.tailmsg/codex-bridge.json status
launchctl print gui/$(id -u)/dev.tailmsg.codex-bridge
tail -30 ~/.tailmsg/codex-bridge/service.log
tailmsg inbox --pending --agent codex
```

`wake <inbox JSON路径>` 只入本地队列并快速返回；`scan` 补扫且执行到期投递；`daemon` 循环扫描；`status` 输出队列状态，不输出消息正文。`uncertain` 需要先检查官方入口的消息记录，确认是否入队，不能直接删除状态并重发。

停止适配器：`launchctl bootout gui/$(id -u)/dev.tailmsg.codex-bridge`。完整回滚：先读取 `~/.tailmsg/codex-bridge/installation.json` 的 `backup` 路径，再运行 `python3 install_codex_bridge.py --rollback <backup路径>`。它恢复那一版代码、指令、配置和服务状态，重新加载既有 `dev.tailmsg`；不删除消息、队列、处理记录或入口会话。

## Linux（user systemd + app-server）

### 前提

安装器和插件都不会启动 Codex。需要事先具备：

- 已安装提供 `codex queue --remote` 的官方 Codex CLI（在 `PATH` 上，或用 `--codex-path` 指定）。
- app-server 需要支持 Unix socket 上的 WebSocket、`initialize` 和 `thread/resume`（含 `excludeTurns`）；已核对 Codex CLI 0.162.1 的本地协议。若版本不支持，常驻状态会报告失败，不会改用其他会话。
- 当前用户自己运行的本机 Codex app-server，监听一个 Unix socket（例如用 `codex app-server --listen unix://PATH` 启动的 `PATH`）。只接受 `unix:///绝对路径`；不支持 `ws://`/`wss://` 等网络端点，也不使用 CLI 的隐式默认 `unix://`。
- 该 app-server 中一个已存在的入口会话 UUID。安装器不创建会话，不使用 `--last`，也不接受会话名称。
- 可用的 user systemd 管理器（`systemctl --user` 可用；需要在登出后继续运行时启用 lingering）。非交互 shell 可能还需设置 `XDG_RUNTIME_DIR`。
- 已用 `install-runtime` 安装到数据目录的 Tailmsg 接收端；接收端服务名按 `service-template systemd-user` 的约定为 `tailmsg.service`。

### 固定会话必须保持加载

仅有监听中的 socket 和 `codex queue` 的 `enqueued` 回执还不够：没有保持订阅的客户端时，app-server 可能卸载固定会话，入队内容因而一直等待。Linux 的桥接 `daemon` 现在会连接同一个 socket，完成 `initialize` → `initialized` → `thread/resume`，并持续保持连接。恢复只指定安装时保存的 UUID 和 `excludeTurns: true`，不传入权限、工作目录、模型或指令覆盖。通知只在内存中读取和丢弃，不记录会话正文；收到审批或交互请求也不会代答，需要用户在自己的 Codex 客户端处理。

断线后按 1、2、4 秒递增退避重连，上限 60 秒；重新连接只恢复同一个 UUID，不重复入队、不确认原信、不新建模型回合。已入队的通知等待 Codex 宿主调度；连接、订阅或重新加载会话都不能证明回合已经开始。空闲连接使用 ping/pong 和状态心跳；同一数据目录通过进程锁只运行一个 bridge daemon。`wake`、单次 `scan`、`status` 和会话名称查询均不会启动常驻连接，Linux 部署必须保持 daemon 服务运行。

创建入口会话时，先以与 app-server 相同的 `CODEX_HOME` 启动一次 Codex 交互会话，或执行一次明确的初始化请求（例如 `codex exec "这是固定消息入口；等候后续消息，不执行其他任务。"`），记录其实际 UUID。选择用户认可的工作目录、权限和工作流；把该 UUID 交给安装器。已有持久会话可以直接使用，无需重建；不要把一次 `thread/start` 返回的空会话误认为已经持久化，也不要把会话标题当成 UUID。

升级已有 Linux 桥接后，重新运行下方安装命令（同一个 UUID、socket 和账户），让服务加载新版适配器。此前安装的临时常驻辅助程序可在确认 `resident.state` 为 `connected` 后停用。重启 app-server 或桥接会暂时断开常驻连接，Codex 对运行中任务的处理仍遵循其原生行为。

### 快速开始

```sh
Y="/path/to/yobiru/scripts/yobiru"
SOCK=/absolute/path/to/app-server.sock      # 你的 app-server 实际监听的 socket
sh "$Y" codex-bridge install --thread-id <入口会话 UUID> --codex-remote "unix://$SOCK" --dry-run
sh "$Y" codex-bridge install --thread-id <入口会话 UUID> --codex-remote "unix://$SOCK"
sh "$Y" codex-bridge status
```

`--dry-run` 只检查并打印计划（含将写入的 unit 文件），不写任何文件，也不调用 `systemctl`。正式安装把适配器复制到 `<数据目录>/codex-local/`（不从插件缓存运行），写入 `codex-bridge.json`、`agents.codex.wake_adapter`、安装回执和 `~/.config/systemd/user/tailmsg-codex-bridge.service`（遵循绝对路径的 `$XDG_CONFIG_HOME`），然后 `daemon-reload`、`enable`、`restart` 该服务，并对 `tailmsg.service` 执行 `try-restart`（只在它已运行时重启）。路径中的空格、`%` 和 `$` 会按 systemd 规则转义。

`--no-start` 只写文件，完全不调用 `systemctl`；之后自行执行 `systemctl --user daemon-reload && systemctl --user enable tailmsg-codex-bridge.service && systemctl --user restart tailmsg-codex-bridge.service && systemctl --user try-restart tailmsg.service`。用 `restart` 而不是 `enable --now`，这样已运行的服务也会加载升级后的代码。

### 权限与账户

- 只使用属于当前用户的 socket；socket 不存在、不是 socket、属于其他用户、所在目录可被他人替换，或没有进程监听（残留的旧 socket）时，都不会调用 `codex queue`，消息记为已知未发送并自动退避重试。
- 核对（`uncertain` 时读取入口会话）使用短时只读的官方 `codex app-server --listen stdio://`，它从 `CODEX_HOME` 读取会话。为保证读的是目标 app-server 的同一账户和会话存储，安装器总是固定记录 `codex_home`：`--codex-home`，否则已安装的值，首次安装时再看 `$CODEX_HOME`，最后是 `~/.codex`。之后在其他 `CODEX_HOME` 环境里升级或触发 wake 都不会改变它。`codex_path` 同样优先保留已安装的值。
- 安装器新建的运行文件和目录（适配器副本、`codex-bridge.json`、安装回执、备份、状态目录）使用仅当前用户可访问的权限（`umask 077`，文件 0600）；已有的 `config.json` 保留原权限，systemd unit 为 0644（不含机密）。服务以当前用户运行，不需要 root。
- `allow_replies` 首次安装为 false；升级时保留已授权的设置，`--no-allow-replies` 可显式关闭，`--allow-replies` 仅在用户明确授权后使用。
- 既有顶层 exec/webhook adapter、其他 agent 及未知字段原样保留；只设置 `agents.codex.wake_adapter`。已有不同的 codex adapter、不同的入口 UUID、混合 UUID 的队列、或已被另一数据目录占用的 user unit 时，安装在写入任何文件之前拒绝。

### 入口会话的沙箱写权限

receiver 和 bridge 服务能收件、入队，不代表 Codex 入口能写入工作区之外的消息目录。使用 `sandbox_mode="workspace-write"` 时，入口至少需要 `<tailmsg_home>/inbox`（ack 的临时文件和原子替换）、`<tailmsg_home>/requests`（跟踪请求的账本及待发送响应）及 `<bridge_home>/handled`（去重、派发和回复记录）的额外写权限。带附件的回复还需要 `<tailmsg_home>/outbox`。跟踪请求的 `request respond`/`reply` 只写本机账本，由接收服务负责投递，不需要网络；旧格式的直接回复和下载附件才需要网络。ack 和 handled 记录不需要网络。`allow_replies=false` 始终禁止自动回复，网络权限不会改变这项授权。

先在可信本机安装环境中准备好配置、inbox、outbox 和 handled 目录，使用配置实际记录的绝对路径。不能把整个数据目录或 bridge 状态目录设为可写，也不能把它们的父目录当作可写工作区，否则入口能修改访问策略、`allow_replies`、运行代码或队列。`approval_policy="never"` 不会额外授予权限。修改 app-server 的默认值后仍需核对既有入口会话实际生效的权限。

完整路径表、最小权限启动示例和验收步骤见 [Codex sandbox setup](docs/CODEX_SANDBOX.md)；插件内同一文档位于 `scripts/codex_bridge/CODEX_SANDBOX.md`。写入失败时停止并保留待办，不能把 handled 记录改存到其他工作区，或绕过沙箱。适配器不会自动调整这些权限。

### 状态含义与 `uncertain` 排查

`enqueued` 只表示官方 CLI 返回了该会话的入队回执，不表示模型已处理、已开始或任务完成；完成与否要看入口会话的记录、产物或回复。`delivered` 只表示在固定会话的用户消息里看到了 `[TAILMSG:<id>]` 标记，即通知已到达，同样不是开始或完成的证据。

Linux 状态还单独显示 `resident.state`：`connected` 表示同一 UUID 已恢复且连接心跳新鲜；`connecting` / `reconnecting` 表示正在连接或退避；`stopped` 表示已停止；`unknown` / `stale` 表示没有有效的新鲜状态。`connected` 仍不证明任务完成。`resident.reason` 只包含固定错误码，例如 `resume_rejected`、`thread_mismatch`、`handshake_invalid`，不泄露服务端错误正文。socket 是 `listening` 但 resident 不是 `connected` 时，检查 user service、账户是否一致、UUID 是否持久存在以及 Codex 版本，而不是清空队列重发。

`sh "$Y" codex-bridge status` 给出通道、socket 状态（`listening`、`socket_missing`、`socket_stale` 等）、队列各状态计数、`retry`/`uncertain` 的原因和处理建议，不输出消息正文。`retry` 表示确定没有发出，会自动重试（退避最长约一小时一次）。`uncertain` 表示 `codex queue` 已启动但超时、失败或回执无效：消息可能已进入会话，因此绝不自动重发。适配器会定期在固定入口会话（`codex_home` 对应的账户）中查找 `[TAILMSG:<id>]` 标记（每轮所有待核对消息共用一次有界的近期历史读取），找到即记为 `delivered`（仅表示通知已到达）。长期找不到时，请在该会话中人工确认后用 `inbox --pending` 处理原信；不要删除队列或状态来强制重发。

```sh
systemctl --user status tailmsg-codex-bridge.service
journalctl --user -u tailmsg-codex-bridge.service -n 30 --no-pager
```

### 回滚

读取 `<数据目录>/codex-bridge/installation.json` 中的 `backup`，然后：

```sh
sh "$Y" codex-bridge rollback <backup 路径> --dry-run
sh "$Y" codex-bridge rollback <backup 路径>
```

回滚只接受该数据目录 `backups/` 下由 Linux 安装器生成的备份，并校验清单中的目标路径。它先 `disable --now` 并确认服务已停止（失败，或服务仍在运行但 unit 文件已不存在时，什么都不恢复），再恢复代码、配置、unit 和回执，按安装前记录的启用/运行状态恢复服务，并 `try-restart` 接收端。若当前 unit 已属于另一个数据目录则拒绝。`--no-start` 只恢复文件、不碰服务（适合已手动停服时）。inbox、SQLite 队列、处理记录和入口会话都不会被删除；保留的队列继续锁定原入口 UUID。

### 执行证据与诊断

`codex_bridge.py --config <配置> diagnose`（以及 `yobiru codex-bridge status` 输出中的 `diagnostics`）只读地汇总可核对的证据，不新建、迁移或修改应用数据，不发送或重发任何消息（SQLite 可能自行创建账本的 WAL 协调文件）；`status` 也改为只读，不再打开可写队列。证据按层次区分：

| 层次 | 来源 | 含义 |
| --- | --- | --- |
| 接收端接收 | 请求账本 `received_at` | 接收端已持久保存请求 |
| 入队 | 桥接队列 `enqueued`、`queue_id`、`enqueued_at` | 官方 CLI 接收了通知 |
| 通知已到达 | `delivered`、`seen_at` | 会话用户消息中出现标记 |
| 实际开始 | 账本 `started_at` / `progress_at` | 唯一 owner 报告了 `running` |
| 最终结果 | 账本 `final_at`、`final_response_id` | 最终结果已持久保存 |
| 结果送达 | 该最终响应的 `delivered_at` | 发件方接收端确认收到，不等于有人已读 |

`execution_unknown` 表示以下情况之一：入队结果未知（`delivery_unconfirmed`）；已入队但超过阈值仍没有开始证据（`accepted_without_start`）；已报告 `running` 但长时间没有新进度（`running_without_recent_progress`）；已过截止时间仍没有最终结果（`deadline_passed_without_final`）。这些都只是诊断，不会把请求标记为失败或取消，不会 ack、重发或另建目标。账本里持久的开始与最终结果优先于旧的传输状态、本机 ack 或暂时缺失的 inbox 文件；超时后的 ack 不能把结果变成已知。超时后的请求只能由其 owner 核对实际效果后，用 `request respond` 记录真实结果来清除该诊断，绝不能靠重做任务来清除。阈值默认 900 秒（开始）和 3600 秒（进度），可在 `codex-bridge.json` 中用 `start_warning_seconds`、`progress_warning_seconds` 调整。

`capabilities` 如实说明能力缺口：桌面传输（`codex_desktop`）没有常驻连接，桥接无法保持会话加载、无法看到限流（例如 429）或会话被取消订阅，也无法重启其消费者；已入队的消息长时间没有开始时，只能由用户打开 Codex 桌面应用和入口会话处理。Linux 常驻连接也只能观察固定会话的卸载事件。入队之后到达截止时间的通知无法撤回，入口必须先检查 `request status` 再行动。所有平台都不会自动重发 `uncertain` 或已入队的消息。

守护进程把每轮扫描、投递和观察作为独立阶段执行，一个阶段失败不会阻止其他阶段；它在 `<bridge_home>/daemon.json`（0600，只含时间、阶段结果和异常类型）写入心跳。心跳陈旧或缺失时，状态显示 `daemon_not_confirmed`，表示忙碌或已停止，而不是正在运行。守护进程每 30 秒以有界批次结束已确定的记录的观察：未跟踪的消息在 ack 或删除后结束；跟踪请求只有在最终结果已持久保存且该最终响应已确认送达时才结束，ack 或文件缺失本身不会结束它。来自其他数据目录、会话或主机的旧记录不会被用来判断本配置。

升级运行时本身不会启用这些桥接保护：需要用同一 UUID 重新运行桥接安装器（`--no-start` 只写文件，服务重启后才生效）。另行比较已安装文件与 `installation.json` 记录的哈希；`status` 只检查守护进程最近是否在报告，不能证明正在运行的代码版本。

## 按消息类型分流（可选，macOS 与 Linux 通用）

已有部署可以让某些消息类型不进入固定入口会话，而交给另一个本机程序处理。在 `codex-bridge.json` 中设置：

```json
{
  "excluded_kinds": ["example-kind"],
  "excluded_kind_adapter": ["/absolute/path/to/handler", "--fixed-arg"],
  "excluded_kind_timeout_seconds": 60
}
```

- `excluded_kinds`：信封 `kind` 字符串列表（每项 1–32 个字符，无控制字符）。`excluded_kind_adapter`：可信本机程序的 argv，第一项必须是绝对路径。`excluded_kind_timeout_seconds`：可选，默认 60，范围 (0, 300]。实际时限取它与“接收端 `hook_timeout_s`（默认 120）减 10 秒”中的较小值，保证适配器在接收端终止它之前先终止处理程序；剩余时间不足 1 秒时不启动处理程序。只读取、不修改用户配置。任一字段无效时，适配器和安装器在任何副作用之前拒绝运行。
- 先完成与普通消息相同的校验（inbox 路径、Tailscale 来源、访问策略、目标 agent `codex`、实际 wake 级别、是否已 ack），之后才按 `kind` 分流。被排除的消息从不进入 Codex 队列：`wake`、`scan` 都不会入队；在配置前已入队的 pending/retry/uncertain 记录，在投递时重新校验并记为 `skipped`（`excluded_kind`）。
- 只有接收端对一封未 ack、实际级别为 `now` 的消息直接调用 `wake` 时，才运行该程序：argv 原样，只追加经过校验的规范 inbox 路径；不经 shell，stdin 为空，不展开任何正文。`none`、`batch`、已 ack，或未配置程序时只记为 skipped，消息留在 inbox。`scan`、定时投递、`status` 和核对都不会运行它。
- 结果写到标准输出（JSON），并在标准错误输出一行固定状态，接收端会把它记入 wake 日志：`forwarded`（程序退出码 0）；`forward_failed`（程序非零退出，`exit_code` 记录其值）；`forward_timeout`（超时，程序及其整个进程组被终止）。这三种情况程序都已启动过，适配器一律以 0 退出，接收端不会重放它；程序失败或超时都不会被自动重试，消息仍留在 inbox 中，由该程序或用户处理。程序未能启动时（`forward_not_started`：可执行文件不可用为 127，没有可用时限为 1）以非零退出，接收端可能按 `wake_attempts` 再试，这是安全的，因为程序从未运行。程序可能在失败或超时前已部分执行，去重与重试由它自己以消息 ID 为键负责；这里不承诺恰好一次。
- 跟踪请求被分流给处理程序时，已完成、已过期或已报告 `running` 的请求不会再次启动处理程序（`skipped`，原因 `request_*`）。`allow_replies` 只约束 Codex 入口；处理程序是用户自己的程序，须自行遵守回复授权。
- 支持边界：这种分流适合用户自行选择的、固定的只读处理程序（例如定时状态查询），用来避免依赖桌面会话；Yobiru 不评估来信正文、不扩大权限、不包含业务逻辑，也不承诺恰好一次执行。`batch` 消息和扫描不会运行处理程序。
- 没有对应的 CLI 选项：直接编辑 `codex-bridge.json`，再用 `codex-bridge install ... --dry-run` 预览（会校验这些字段，并显示 `excluded_kinds`）。两个安装器升级时都保留这些字段，以及调整过的轮询/重试时间、自定义入口指令路径、已记录的 Codex CLI 路径、固定的 Codex 账户设置和已授权的 `allow_replies`。迁移已有部署时，先把原有分流设置写入 `codex-bridge.json`，确认预览无误后再安装，避免消息被误投到入口会话。`codex-bridge status` 会列出当前分流设置，但不会运行程序。

## 会话名称与发现（0.3.0）

安装桥接后，`yobiru agents` 和其他机器的发现（`GET /info`，仅对访问策略允许的认证机器）会自动报告固定入口会话的 UUID。原生名称由 `codex_bridge.py session-name` 只读查询：短时官方 app-server 执行 `thread/read`（`includeTurns: false`），只取 `Thread.name`，从不读取 `preview`、回合或会话列表，不入队、不创建队列数据库、不启动模型回合；接收端在整组进程超时清理和输出上限下调用它并缓存约 5 分钟。别名用 `yobiru session bind --agent codex --display-name 名称` 记录在 `sessions.json`；与桥接入口不同的 UUID 会被拒绝。两个安装器升级时不碰 `sessions.json`，名称因此保留。

## 入口指令的升级

两个安装器在首次安装时把公开模板复制为 `<数据目录>/codex-local/CODEX_DISPATCHER.md`。之后升级时，若该文件已存在，就原样保留其内容（可能是用户自己的工作流），只更新代码；安装输出和 `--dry-run` 会在模板有差异时提示。`dispatcher_instructions` 指向其他路径时，安装器既不写该文件，也不改该配置。升级前的文件会进入备份，回滚恢复的正是原来的内容。要采用新模板，请自行比较插件中的 `scripts/codex_bridge/CODEX_DISPATCHER.md` 与本地文件，审阅后手动合并。

## 验收

离线：`python3 -m unittest -v test_codex_bridge test_codex_queue_client test_codex_resident test_install_codex_bridge test_install_codex_bridge_linux`。Linux 测试使用临时 AF_UNIX socket、假的 `codex` 与 `systemctl` 可执行文件和隔离的 HOME，不连接真实 app-server、不调用真实 systemd。

桌面会话继续遵守 Codex 原生权限策略。入口能够请求查找、转交和创建任务；如果原生工具要求审批，消息会保留在该入口等待用户处理，适配器不会代替用户批准。部署与真实验收结果见 [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md)。

真实验收应在独立 Tailmsg home/端口接收一封本机发给本机的消息，不放宽生产访问策略。确认真实身份收件 → 同一个入口会话读取 → 派生任务写出指定测试产物；第二封请求找到并继续该已有任务。重复扫描和服务重启不能重复派生。HTTP 202、本地排队成功和模型完成必须分别核对。
