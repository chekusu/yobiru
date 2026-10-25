# 新しいマシンで Yobiru をセットアップする

[English](SETUP.md) | [简体中文](SETUP.zh-CN.md) | **日本語**

Yobiru プラグインのインストール自体は何も実行しません（インストールフックはありません）。動作には Tailscale、設定、常駐受信プロセス、各エージェントの起動方法、そして（任意で）各エージェントが使うセッションが必要です。`yobiru onboard` はそれらをすべて確認し、このプラットフォーム向けの次の手順を表示します。読み取り専用で、何もない状態でも動きます。エージェントに「Yobiru をセットアップして」と頼むと、`yobiru-setup` スキルが同じ手順で進めます。

以下の Tailscale の情報は公式ページ（2026-10-11 確認）に基づきます：[Linux](https://tailscale.com/docs/install/linux)、[macOS](https://tailscale.com/docs/install/mac)、[Windows](https://tailscale.com/docs/install/windows)、[ダウンロード](https://tailscale.com/download)、公式 Linux スクリプト <https://tailscale.com/install.sh>。

## コマンドの実行方法

プラグインは PATH に `yobiru` プログラムを置きません。以下の `yobiru` は、README の `Y` の書き方と同じく、プラグインの起動スクリプトを引用符付きの絶対パスで呼び出すことを表します：

```sh
Y="/absolute/path/to/yobiru/scripts/yobiru"     # インストール済みプラグインまたは展開したリリース内
sh "$Y" onboard
```

Windows では `py -3 "C:\path\to\yobiru\scripts\yobiru.py" onboard`。既定以外のデータディレクトリはコマンドの前に指定します：`sh "$Y" --home /workspace/yobiru-data onboard`。

## 役割分担

| 手順 | エージェント（セットアップを依頼された後） | あなた |
|---|---|---|
| Tailscale のインストール | ホストの root/sudo で公式 Linux スクリプトを実行 | macOS/Windows アプリのインストールと承認、管理者が必要な操作 |
| サインイン | Linux で `tailscale up` を実行し、ログイン URL を示す | URL（またはアプリ）を開き、アカウントと tailnet を選ぶ。認証キーはチャットに貼らない |
| 相手と名前 | 計画を適用 | 誰が送信できるか、エージェント名を決める |
| 受信サービス | 表示された定義をインストールして起動 | — |
| 起動方法とセッション | 選ばれたアダプターと紐付けを設定 | 各エージェントの既存セッションを指定 |

Yobiru は Tailscale の ACL、shields-up、DNS を変更しません。新規インストールでは、Tailscale が認証し、ネットワークポリシーが接続を許可し、ローカル拒否ルールに一致しない相手と通信できます。通信の許可はタスク実行や起動の許可にはなりません。`help`、`doctor`、`status`、`onboard`、`agents`、`update check` は読み取り専用です。

## 1. Tailscale

- **Linux**：root または sudo で `curl -fsSL https://tailscale.com/install.sh | sh` を実行し、`sudo tailscale up` でログイン URL を表示します。root/sudo がない（管理者に依頼）、`/dev/net/tun` がない（コンテナや一部のクラウドサンドボックス：userspace networking では受信プロセスが使う Tailscale アドレスが得られないので、TUN が使えるホストを使う）、`tailscaled` を常駐させる systemd がない（別の監視手段で起動）場合、`onboard` は推測せずに阻害理由を報告します。
- **macOS**：Mac App Store またはダウンロードページから Tailscale アプリを入れ、システム拡張／VPN 構成の確認を承認し、メニューバーからサインインします。アプリの CLI は `/Applications/Tailscale.app/Contents/MacOS/Tailscale` です。`tailscale` が PATH にない場合は `yobiru setup --tailscale-cmd /Applications/Tailscale.app/Contents/MacOS/Tailscale`（既存の設定では `--replace tailscale_cmd` を追加）。
- **Windows**：管理者権限でインストーラーを実行し、通知領域のアイコンからサインインします。CLI が PATH にない場合：`yobiru setup --tailscale-cmd "C:\Program Files\Tailscale\tailscale.exe"`。PowerShell では `& "C:\Program Files\Tailscale\tailscale.exe" status` のように呼び出します。`py -3 "<plugin>\scripts\yobiru.py"` を使います。Windows では Codex ブリッジは使えません。

`doctor` は見つけた状態を示します：未インストール、デーモン停止、権限不足、未ログイン、停止中、デバイス承認待ち（tailnet 管理者が承認）、稼働中。

## 2. 相手とエージェント名

```sh
yobiru setup --agent NAME --self-agent NAME --default-agent NAME          # 計画
yobiru setup --agent NAME --self-agent NAME --default-agent NAME --apply
```

新規設定は `tailnet` モードで、相手ごとの許可リストは不要です。新規インストールで相手を限定するには `--allow PEER`（ホスト名、FQDN、`tag:NAME`、`user:LOGIN`）を追加します。`--send-only` は空の許可リストを使います。既存のアクセスポリシーは維持され、ID の変更には `--replace FIELD` が必要です。変更前の設定はバックアップされます。

既存インストールでは `yobiru policy show` で実際のモードを確認します。従来の許可リストを移行するには、`yobiru policy set --mode tailnet` をプレビューし、所有者の許可があれば `--apply` を付けます。`--deny RULE` は拒否リスト全体を置き換え、複数のルールは繰り返して指定します。どちらのモードでも拒否が優先されます。0.4 の受信プロセスとブリッジは次の認可確認時に新しいポリシーを読むので、ポリシーだけの変更に再起動は不要です。最初のランタイム更新には再起動が必要です。[0.4 移行ガイド](UPGRADING-0.4.md)を参照してください。

## 3. 常駐受信プロセス

```sh
yobiru install-runtime --apply
yobiru service-template systemd-user     # または systemd、launchd、windows（onboard が適切なものを示す）
```

表示された定義をヘッダーの指示どおりに配置して起動します。受信するには shields-up を無効にし、tailnet のポリシーで選んだ相手から `tcp:7766` に届くようにする必要があります。

## 4. 各エージェントの起動方法と担当セッション

この二つは別物です。起動：Codex ブリッジ（`yobiru codex-bridge install --thread-id UUID ...`）または `yobiru wake-config --agent NAME --exec PROGRAM | --webhook ...`。アダプターがなければメッセージは `inbox --pending` で待ちます。セッション：`yobiru session bind --agent NAME --session-id 正確なID [--display-name 別名] [--confirmed-title タイトル]`、予行の後に `--apply`。名前だけでセッションを選んだり改名したりはしません。Codex ブリッジがある場合は窓口セッションの UUID が自動的に表示され、矛盾する紐付けは拒否されます。

Codex の窓口で `workspace-write` を使う場合は、[最小限のサンドボックス権限](CODEX_SANDBOX.md)も設定します。実際の絶対パスで inbox と handled の書き込みを許可し、添付ファイル付き返信の場合だけ outbox も許可します。これらのディレクトリは信頼できるセットアップ環境で先に作成し、ポリシー・実行コード・キューは読み取り専用に保ちます。データディレクトリ全体を書き込み可能にしないでください。ネットワークは許可された返信や添付ファイルの取得に必要ですが、`allow_replies=false` を上書きする権限ではありません。

## 5. 確認

```sh
yobiru doctor
yobiru agents --online
yobiru send PEER/AGENT "hello"
```

`agents` は各マシンの受信状態、送信できるか、Yobiru のバージョン、各エージェントのセッション名（出所付き）と正確な ID を示します。起動を試すには、起動ポリシーで `now` が許されている相手から `--wake now` で送り、受信側で `show ID` の `wake_delivery` を確認します。

## その後の更新

プラグインはインストールした方法で更新します：Codex／Cursor／Grok Bot のプラグインページやマーケットプレイス、新しい公式リリースアーカイブ（`SHA256SUMS` で検証し、データディレクトリの上ではなく新しいフォルダーに展開）、または公式タグのチェックアウト。その後は新しいパッケージの `scripts/yobiru` を使います（`yobiru version` で新バージョンを確認）。データディレクトリには触れません。

`yobiru-upgrade` スキルと `yobiru update --help` を参照してください。プラグインの更新は常駐ランタイムを変えません（`install-runtime --upgrade --apply` の後に受信プロセスを再起動）。このマシンが公開済みリリースで動くようになったら、`yobiru update notify` が他の既知のインストールに知らせます。

既存インストールは元の許可リストを維持し、ポリシー移行とソフトウェア更新は別です。`update maintain --apply` は更新確認を記録し、既存のアップグレード通知だけを再試行します。15 分ごとと起動後に実行するには、`update reminder-template systemd-user --retry-notices`（または `launchd`／`cron`）を表示し、手順どおりに任意ジョブをインストールします。プラグインのパス変更後は再生成します。ソフトウェアのインストールやポリシー変更は行いません。[0.4 移行ガイド](UPGRADING-0.4.md)を参照してください。
