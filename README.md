# Codex Helix Editor

**PVE上のLinux VMに置く、個人用のHelix Floorプリセット作成MCP。**

Codex Desktopが曲の情報と音色の設計を担当し、このサーバーが実機Exportの調査・候補プリセット生成・Windowsへの受け渡しを担当します。サーバー自身はOpenAI APIを呼ばないため、別のOpenAI APIキーは不要です。

## 現在の到達点 — v0.2.0

Streamable HTTPのMCPサーバー、Windowsワーカー、Docker Compose、導入手順を実装しています。

**Windowsへの受け渡しは、手動インポート用ファイルの配置までです。HX Editの指定スロットへ自動インポートするGUI adapterは未実装です。** `stage_preset_for_windows`の完了は`manual_import_required`で、`hardware_applied`は`false`です。実際のWindows画面を確認して自動登録部分を追加する前提を、成功表示に混ぜていません。

| 機能 | 状態 |
| --- | --- |
| CodexからHTTPSでMCPへ接続 | 実装。SDKによるStreamable HTTP |
| 実機Exportの登録・既存モデルと値の調査 | 実装 |
| 確認済みパラメータだけを変更した候補.hlxの生成 | 実装。元のモデル・ルーティングを保持 |
| 名前・音色データの再Export比較 | 実装。保存先・聴感の確認とは別 |
| HX Editの検出・起動・UI診断 | Windowsワーカーに実装。実機Windowsでは未検証 |
| 候補をWindowsへ送り、対象セットリスト／スロットと一緒に保存 | 実装。手動インポート補助 |
| 曲の自由な音響解析・任意モデル構成の生成 | この版には含まない。Codexの設計と実機テンプレートを利用 |
| 指定スロットの無人選択・自動Import | Windows画面で検証する次段階 |

## 構成

| 場所 | 役割 | 接続 |
| --- | --- | --- |
| WindowsのCodex Desktop | 調査、音色設計、MCPツールの呼び出し | VMの`https://<host>/mcp`へ接続 |
| PVE上のLinux VM | Caddy、MCP、SQLite、ジョブ管理 | HTTPS 443を受け付ける |
| Windowsのログインユーザー環境 | `helix-worker`、HX Edit | VMへアウトバウンドHTTPSでpoll |
| Helix Floor | 実際の音声処理・プリセット保存 | Windows PCへUSB接続 |

MCPをLinux VMへ置いても、LinuxからWindowsのHX Edit画面を直接操作できるわけではありません。この構成ではWindowsワーカーを間に置きます。PVEへのUSBパススルーやWindowsへの受信ポート開放は基本構成に含めません。

## 導入する順番

1. **[PVE VMへのデプロイ](docs/DEPLOY_PVE.ja.md)** — VM、Docker、保存領域、秘密ファイル、HTTPS、起動・確認。
2. **[Windowsワーカーの設定](docs/WINDOWS_WORKER.ja.md)** — Python、トークン、証明書、ログイン時起動。
3. **[Codex Desktopへの登録](docs/CODEX_SETUP.ja.md)** — URL、Bearer環境変数、CA証明書、接続確認。
4. **[音色作成ワークフロー](docs/WORKFLOW.ja.md)** — 実機テンプレートの登録から候補生成と手動インポートまで。

ローカル開発ではPython 3.12以降を使います。

```sh
python -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m unittest discover -s tests -v
```

Windowsでは`.venv\Scripts\python.exe`を使います。サーバー起動には、手順書のMCP用・worker用の別々の秘密ファイルが必要です。空の秘密値やデフォルトパスワードでは起動しません。

## MCPツール

| ツール | 内容 |
| --- | --- |
| `service_status` | 設定と実装済みの能力。実機接続済みという意味ではない |
| `list_templates` | 登録済みテンプレートを最大200件表示 |
| `register_template` | 実機ExportのUTF-8 JSONを登録。最大256 KiB |
| `inspect_template` | 観測されたモデルID・生の値・非対応割当を調べる |
| `create_edit_plan` | 元ファイルのSHA-256に紐づく空の変更計画 |
| `build_candidate` | 計画に従って候補を生成し保存 |
| `get_candidate` | 候補の.hlx JSON、ハッシュ、変更記録を取得 |
| `compare_readback` | HX Editで再Exportしたデータを候補と比較 |
| `worker_status` | Windowsからの最終poll時刻 |
| `queue_hx_action` | `discover` / `launch` / `inspect`の限定ジョブ |
| `stage_preset_for_windows` | 候補と登録先manifestをWindowsの固定spoolへ配置 |
| `get_job` | ジョブの状態と結果を取得 |

`design_lead_tone` MCP promptも提供します。これはCodexへ音色設計の進め方を渡すもので、サーバーが曲を聴いたり生成AIを呼び出したりする機能ではありません。

## 日々の依頼例

初回設定と実機テンプレートの登録が終わった後の依頼です。

> 「○○の『△△』の2:10からのリードを、登録済みのギターとヘッドホン用に作成して。候補をいつものWindowsへ送って」

v0.2では最後に手動インポートが必要です。自動Import adapterを実機で検証して追加した後、「作って登録して」で指定スロットへの反映までつなぐ設計です。

## 認証とデータ

- 個人用の固定Bearer認証です。OAuth認可サーバーではありません。
- MCP用とworker用に別トークンを使い、対応ルートだけで受け付けます。
- Host／Originを検証し、VMへの接続はCaddyでHTTPSにします。
- Windowsジョブから任意のコマンド、実行ファイル、保存先を指定できません。
- 候補・テンプレート・ジョブはVMのSQLiteに保存。workerにはspoolと実行journalを保存します。
- 期限切れleaseは`unknown`となり、自動再配信しません。通信失敗を操作失敗と決めつけないためです。
- `.env`、トークン、DB、ユーザープリセット、IRをGitへ登録しません。

## 検証範囲

[VALIDATION.md](VALIDATION.md)に今回のソフトウェア検証と未確認部分を記録しています。人工fixtureによるファイル・プロトコル・ジョブの検証は、実機の音色やGUIの確認を代替しません。

## 参考

- [OpenAI — MCP](https://learn.chatgpt.com/docs/extend/mcp)
- [MCP — Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [Line 6 — HX Editマニュアル](https://line6.com/support/manuals/hxedit/)

Line 6のファクトリープリセット、モデル定義、IRデータは同梱していません。.hlxの編集器は限定的な互換ツールで、Line 6公式APIや完全なDSP検証器ではありません。
