# 検証記録

v0.2.0 — 2026-11-11

## 確認した範囲

Linux / Python 3.12環境で、以下を人工fixture、ローカルHTTP処理、実際のloopback TCP接続で確認しています。

- 元プリセットと未知フィールドの保持、型・範囲・ハッシュ・モデル・Snapshot／Controllerの制約。
- MCP initialize、tools/list、構造化ツール結果。
- MCP用／worker用トークンの分離、Host／Origin、入力容量。
- 実機Export登録の模擬データから候補生成、実際のworker staging handler、ジョブ結果までの結合。
- 同じrequest_idの再投入、同時leaseの制限、期限切れのunknown化、結果再送。
- SQLiteオンラインバックアップと読戻し。
- Windowsワーカーの固定保存先、SHA-256、journal、許可した処理、通信リトライ。

## 実行結果

| 確認 | 結果 |
| --- | --- |
| `PYTHONPATH=src python3 -m unittest discover -s tests -v` | 39件成功。preset 13、server 5、storage 5、worker 16 |
| Uvicornを実際に起動し、`scripts/smoke_mcp.py`から公式SDKで接続 | 成功。protocol `2025-11-25`、12ツール列挙、`service_status`取得 |
| wheelビルドと隔離ディレクトリへのインストール | 成功。バージョン0.2.0、CLI import、PowerShellリソースの同梱を確認 |
| `bash -n deploy/prepare-vm-storage.sh` | 成功 |
| Compose YAML、保存領域、UID、ポート、文書リンクの静的確認 | 成功。Dockerデーモンでの起動確認は未実施 |

TCP確認では、この検証環境のSOCKSプロキシ設定を子プロセスから除き、127.0.0.1へ直接接続しました。通常のHTTPS、Caddyの内部CA、Windowsへのネットワーク接続を検証した結果ではありません。

## 未確認・未実装

- ユーザーのPVEへの実際のVM作成とDocker起動。
- ユーザーのDNS／CA証明書／Codex Desktopへの登録。
- Windowsログインセッションでの実際のPowerShell／HX Edit実行。
- Helix Floorの実データを使ったプリセット生成・読込・試奏。
- 指定スロットへ自動ImportするGUI adapter。

テスト用JSONは実機へ読み込めるプリセットではありません。`hardware_applied`はこの版では常にfalseです。GitHub ActionsはLinux／WindowsのソフトウェアテストとWindows PowerShellの構文解析を定義していますが、実際のGUIやUSBデバイスを検証するものではありません。
