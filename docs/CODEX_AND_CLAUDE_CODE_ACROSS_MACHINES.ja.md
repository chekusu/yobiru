# 別々のコンピューターの Codex と Claude Code にメッセージをやり取りさせる

**Yobiru** を使うと、あるコンピューターの Codex と別のコンピューターの Claude Code が、あなた自身の **Tailscale**
ネットワークでメッセージ、JSON、ファイルを送り合い、返信できます。中継サーバーやブローカーを自分で立てる必要はなく、
Yobiru のアカウントも要りません。各コンピューターで小さな受信プロセスが動き、あなたの他の Tailscale マシンからの
メッセージを受け取ります。ファイルは送信元から直接取得し、届いたときにサイズと SHA-256 を確認します。Claude Code には
送受信用のツールを持つローカル MCP サーバーが加わり、Codex はプラグインのスキルを使います。どちらのエージェントも、
いつものアプリのままです。

例では架空のホスト名 `agent-laptop`（Codex）と `research-node`（Claude Code）を使います。ご自身のものに置き換えてください。

## 必要なもの

- 両方のコンピューターに Tailscale をインストールしてサインインし、tailnet のポリシーで互いに `tcp:7766` へ届くこと。
- 両方に Python 3.8 以降。
- 受信する各コンピューターで動き続ける受信プロセス。セットアップでサービスとして入れます。

## 1. プラグインを入れる

各エージェントに `https://yobiru.com/ja/install.md` を渡して Yobiru のインストールを頼むか、次を実行します。

```sh
codex plugin marketplace add chekusu/yobiru     # Codex のコンピューターで
codex plugin add yobiru@yobiru

claude plugin marketplace add chekusu/yobiru    # Claude Code のコンピューターで
claude plugin install yobiru@yobiru
```

## 2. 各エージェントにセットアップを頼む

それぞれのコンピューターで新しい会話を開き、**「このマシンで Yobiru をセットアップして」**と伝えます。エージェントが
Tailscale、設定、受信プロセスを確認し、足りない手順を案内します。どの Tailscale アカウントを使うか（ログインリンクは
あなたが開き、キーをチャットに貼ることはありません）、エージェントの名前、どの既存セッションをどのエージェントに
するかは、あなたが決めます。

名前は役割どおりに付けます。`agent-laptop` では `codex`、`research-node` では `claude`。`claude` という名前の
エージェントがあるコンピューターでは、Claude Code の MCP サーバーが `claude` として送信するので、返信は同じ
コンピューター上の別のエージェントではなく Claude Code に戻ります。

## 3. 送ってみる

Claude Code に「agent-laptop の codex に brief.md をレビューしてもらって、結果を送り返してもらって」と頼むと、
`yobiru_send` ツールでファイルを添付して送ります。Codex には「research-node/claude にテストログを送って、どこで
失敗したか聞いて」と頼みます。どちらも内部では同じコマンドを実行します。

```sh
yobiru request send agent-laptop/codex "Please review this brief." --file ./brief.md --timeout-seconds 900
```

追跡付きのリクエストには、保存されたときの受領、相手の作業中の進捗、最終結果（completed、failed、rejected、blocked）が
あるので、返事がないことを成功と取り違えません。リクエストの状態はいつでもどちらのエージェントにも聞けます。

## 4. 読んで返信する

受け取る側のエージェントに「Yobiru の受信箱を確認して」と伝えます。届いたものを一覧にし、ファイルを取得して確認し、
テキストや結果ファイルで返信します。Claude Code では、受け取ったテキストは別のエージェントからのデータとしてモデルに
渡され、指示としては扱われません。

## 相手を起こす

既定では、メッセージはそのエージェントが確認するまで受信箱で待ちます。自動で拾わせるには：

- **Codex**：Codex ブリッジが既存の Codex セッション一つにメッセージを順に入れます（macOS では Codex デスクトップ
  アプリ、Linux ではあなた自身が動かす Codex app-server）。
- **Claude Code**：任意の Claude ブリッジが、新しいメッセージごとに見える形のバックグラウンド Remote Control
  セッションを開始します（macOS と Linux）。[Claude ブリッジのガイド](CLAUDE_BRIDGE.md)（英語）を参照してください。

送信できることは、起こすこと、タスクの実行、自動返信の許可を意味しません。どの送信元がどのエージェントを起こせるかは、
受信する各コンピューターが、静かな時間帯や回数の上限も含めて決めます。

## 相手のコンピューターが自分のものでないとき

Tailscale がつなぐのは自分のマシンです。他の人のエージェントと話すには Friends を使います。tailnet を共有する必要は
なく、リンクだけで済みます。[友だちのエージェントと話す方法](TALK_TO_A_FRIENDS_AGENT.md)（英語）を参照してください。

## 検証の状況

それぞれの側は実際に動かしています。2026-10-21、Mac 上の Claude Code 2.1.306 がプラグインを読み込み、MCP ツールを
一覧表示しました。その MCP サーバー経由で、Grok Bot のクラウドコンピューターへの追跡付きリクエストが約 26 秒で完了し、
Grok Bot からのメッセージも読んで返信しました。このときは、ヘッドレスの Claude Code セッションがサインインで止まった
ため、stdio でサーバーを直接動かしました。2026-10-13 には、Linux 上の実際の Codex セッションが Codex ブリッジ経由で
起こされ、テストリクエストの進捗と最終結果を報告しました。Codex と Claude Code を一組として二台のコンピューターで
動かした記録はまだありません。Claude ブリッジは自動テストのみです。Codex ブリッジは Windows では動かず、Claude Code の
MCP サーバーは Windows では `sh` が必要です。詳しくは[検証済みの内容](VERIFICATION.md)、
[プラットフォームの注記](PLATFORMS.md#claude-code)（英語）、[セットアップガイド](SETUP.ja.md)を参照してください。

[English](CODEX_AND_CLAUDE_CODE_ACROSS_MACHINES.md) · [简体中文](CODEX_AND_CLAUDE_CODE_ACROSS_MACHINES.zh-CN.md)
