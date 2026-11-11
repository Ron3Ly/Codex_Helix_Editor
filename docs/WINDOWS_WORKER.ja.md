# Windows workerの設定とHX Editの操作範囲

このworkerは、Linux VMのジョブをHTTPSで取得してWindows上で実行します。Helix FloorはこのWindows PCへUSB接続し、HX EditもWindowsにインストールします。VMへのUSBパススルーやWindowsの受信ポート開放は必要ありません。

**実装済みなのは、HX Editの検出・起動・画面情報の取得と、生成プリセットのローカル配置です。指定スロットへのGUI自動インポートは未実装です。** 名前に`import`を含むジョブも、現時点では候補ファイルを配置して手動読み込みを案内する処理です。

## 前提

- WindowsでPython 3.12以降が動くこと。
- HX Editがインストールされ、手動でHelix Floorを認識できること。
- [Codex接続の手順](CODEX_SETUP.ja.md)で、worker専用tokenとCaddyのCA証明書を転送済みであること。
- VMのホスト名とTCP 443へWindowsから到達できること。
- HX Editとworkerを、同じログインユーザーの対話セッションで動かすこと。

WindowsサービスのSession 0では対話ユーザーの画面操作を前提にできません。このworkerはログインしたユーザーのPowerShellから起動し、後述の自動起動もログオン時に設定します。[Microsoftのサービスと対話セッション](https://learn.microsoft.com/en-us/windows/win32/services/interactive-services)

## 1. リポジトリとPython環境を用意する

Windowsの通常のPowerShellで、保存したい作業ディレクトリから実行します。

```powershell
git clone https://github.com/Ron3Ly/Codex_Helix_Editor.git
Set-Location .\Codex_Helix_Editor
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install .
& .\.venv\Scripts\helix-worker.exe --help
```

この例はPython 3.12を指定します。別の対応バージョンを導入している場合は、その実行ファイルで同じvenvを作成してください。PowerShellのvenv有効化スクリプトは使わず、実行ファイルを直接呼ぶので、そのために実行ポリシーを変更する必要はありません。

## 2. 環境変数を設定する

```powershell
$env:HELIX_SERVER_URL = 'https://helix.home.arpa'
$env:HELIX_WORKER_ID = 'helix-floor-windows'
$env:HELIX_WORKER_TOKEN_FILE = Join-Path $env:LOCALAPPDATA 'HelixMcp\secrets\worker_token.txt'
$env:HELIX_WORKER_DATA_DIR = Join-Path $env:LOCALAPPDATA 'HelixMcp\worker'
$env:SSL_CERT_FILE = Join-Path $env:LOCALAPPDATA 'HelixMcp\certs\caddy-root.crt'
```

`HELIX_WORKER_ID`はVMの`.env`と同じ値にします。別のIDはサーバー側で許可されません。`HELIX_SERVER_URL`は`/mcp`を含まないベースURLです。workerはMCP用とは別の`worker_token.txt`を使います。

起動用に、実際のHX Edit実行ファイルの絶対パスを設定します。インストール先はショートカットのプロパティなどで確認し、存在しないパスを推測しないでください。

```powershell
# 実際に確認したパスへ置き換える。
$env:HELIX_HX_EDIT_EXE = 'C:\Program Files\Line 6\HX Edit\HX Edit.exe'
Test-Path -LiteralPath $env:HELIX_HX_EDIT_EXE -PathType Leaf
```

`False`ならパスを修正します。`HELIX_HX_EDIT_EXE`は起動操作に使う固定パスです。サーバーから任意の実行ファイルやシェルコマンドを渡して実行する仕組みはありません。

内部CAを使う構成では、上記の`SSL_CERT_FILE`を指定し、証明書検証を有効にしたまま接続します。公開CAを使う構成ではこの専用CA指定は不要です。既存の企業CAが必要な場合は、それも含むCA bundleを用意してください。

## 3. 起動する

まず一度だけ問い合わせます。

```powershell
& .\.venv\Scripts\helix-worker.exe --once
```

継続運用では次を実行し、PowerShellを開いたままにします。

```powershell
& .\.venv\Scripts\helix-worker.exe --poll-interval 3
```

workerは受信待ちのHTTPサーバーを開かず、VMへ接続してジョブを取得します。停止は`Ctrl+C`です。同じworker ID・データフォルダのworkerを同時に複数起動しないでください。

Codex側で`worker_status`を確認してから、HX Editの検出・起動・診断を順に依頼できます。

> Windows workerの状態を確認して、HX Editを検出してください。見つかったHX Editを起動し、ウィンドウと接続状態を診断してください。

## 実装済みジョブ

| ジョブ種別 | 動作 |
|---|---|
| `hx_discover` | インストール済みHX Editや関連状態を確認 |
| `hx_launch` | 固定された実行ファイルを起動 |
| `hx_inspect` | HX EditのウィンドウやUI情報を診断 |
| `hx_import` | 候補`.hlx`とmanifestをWindowsのspoolへ配置。自動Importは行わない |

検出・診断結果は実機への音色の書き込み成功を意味しません。特に`hardware_applied: false`を成功へ読み替えないでください。

## 保存されるファイル

`HELIX_WORKER_DATA_DIR`の下に、ジョブごとのデータを保存します。

| 場所 | 用途 |
|---|---|
| `spool/<job_id>/preset.hlx` | HX Editで読み込む候補プリセット |
| `spool/<job_id>/manifest.json` | 候補の識別情報・登録先などの情報 |
| `jobs/` | ジョブの実行記録。二重実行を避けるために使用 |
| `diagnostics/` | 検出・診断の結果 |

再起動時も同じデータフォルダを使用します。フォルダを移す場合はworkerを停止してから全体を移し、環境変数も変更してください。journalだけを削除して再試行させる運用は避け、ジョブの状態を確認して新しい要求としてやり直します。

## 手動で読み込んで結果を確認する

候補の配送が完了すると、ジョブには`manual_import_required`が返ります。

1. spoolの`manifest.json`を開き、対象候補と登録先が意図どおりか確認します。
2. HX Editで指定のセットリストとスロットを選びます。既存の音色を置き換える場合は、先にExport Presetまたはバックアップを保存します。
3. HX Editの**Import Preset**から、該当ジョブの`preset.hlx`を読み込みます。
4. 名前・ブロック・割当・音量を確認します。試奏するときは対象プリセットを呼び出します。
5. 読み込んだスロットからもう一度`.hlx`を書き出し、生成候補と`compare_readback`で比較します。比較は音色データの一致確認であり、音の完全一致やスロットの証明ではありません。

HX Editのインポートは選択スロットの内容を置き換えます。読み込みと現在の音色としてのロードは別の操作です。[Line 6 HX Edit公式マニュアル](https://line6.com/data/6/0a00051affb6673cd6b0114b4/application/pdf/HX%20Edit%20Pilots%20Guide%203.80%20-%20Japanese%20.pdf)

## ログオン時に自動起動する場合

手動起動でサーバーとの接続と診断が確認できてから設定します。タスクスケジューラの**タスクの作成**を使い、次のように指定します。

| 項目 | 指定 |
|---|---|
| ユーザー | HX Editを使用する自分のWindowsユーザー |
| 実行条件 | **ユーザーがログオンしているときのみ実行する** |
| トリガー | そのユーザーのログオン時 |
| プログラム | リポジトリ内`.venv\Scripts\helix-worker.exe`の絶対パス |
| 引数 | `--poll-interval 3` |
| 開始フォルダー | リポジトリの絶対パス |
| 同時起動 | 既存インスタンスがあれば新しく起動しない |

タスクからも同じ設定を読めるように、上で確認済みのパス・URLだけをユーザー環境変数へ保存します。tokenの値は保存せず、tokenファイルのパスを保存します。

```powershell
$helixWorkerNames = @(
  'HELIX_SERVER_URL', 'HELIX_WORKER_ID', 'HELIX_WORKER_TOKEN_FILE',
  'HELIX_WORKER_DATA_DIR', 'HELIX_HX_EDIT_EXE', 'SSL_CERT_FILE'
)
foreach ($helixName in $helixWorkerNames) {
  $helixValue = [Environment]::GetEnvironmentVariable($helixName, 'Process')
  if (-not [string]::IsNullOrWhiteSpace($helixValue)) {
    [Environment]::SetEnvironmentVariable($helixName, $helixValue, 'User')
  }
}
```

既に`SSL_CERT_FILE`を別用途で使っている場合は、その設定を保持するか、worker専用の起動スクリプト内だけで設定します。環境変数を保存した後はサインアウト・サインインし、タスクが使用する環境を更新します。タスクに別ユーザーのパスワードを保存したり、管理者権限を一律に付けたりする必要はありません。[Microsoftの対話ログオン型タスク](https://learn.microsoft.com/en-us/windows/win32/api/taskschd/ne-taskschd-task_logon_type)

workerは同梱のPowerShell補助を呼びますが、マシン全体の実行ポリシーを変更しません。組織ポリシーなどで実行できない場合は、その実行方法・署名要件に合わせて配備してください。

## 更新・バックアップ

更新時はworkerを停止し、リポジトリを確認済みのコミットへ更新してから、同じvenvで`python.exe -m pip install .`を再実行します。サーバーとworkerの更新は同じリリースに揃えます。

`HELIX_WORKER_DATA_DIR`、設定したパス、tokenファイルとCAをバックアップします。復元時には同じworker IDとjournalを復元し、サーバー側の未完ジョブを確認してから起動します。VMのSQLiteバックアップだけではWindows上の実行記録は戻りません。

## 自動GUIインポートへ拡張するために残る作業

実際のWindows環境で、HX EditのUI Automation情報、表示倍率、バージョン、接続済みHelix、セットリスト選択、スロット確認、Export/Importのダイアログを検証する必要があります。誤ったウィンドウや未接続状態を検出したら停止し、書き込み後に再エクスポートして確認できる処理を実装してから、`hardware_applied`を成功にできる設計です。

このリポジトリには、未検証の画面座標や推測したクリック列による自動書き込みを含めていません。
