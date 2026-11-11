# WindowsのCodex Desktopから接続する

[PVEへの配備](DEPLOY_PVE.ja.md)が完了し、WindowsからVMへTCP 443で接続できる状態を前提にします。ここで登録するのはこのリポジトリのMCPサーバーです。OpenAI APIキーをサーバーに置く作業はありません。

CodexはStreamable HTTPのMCPサーバーとBearer tokenの環境変数指定に対応しています。設定はユーザーの`config.toml`、または信頼済みプロジェクトの`.codex/config.toml`に置けます。[OpenAI公式MCP資料](https://learn.chatgpt.com/docs/extend/mcp)

## 1. CA証明書と二つのtokenをWindowsへ渡す

VM上の`Codex_Helix_Editor/deploy`で、SSH転送用の一時フォルダを作ります。トークン内容は表示しません。

```bash
umask 077
install -d -m 0700 "$HOME/helix-client-transfer"
sudo install -m 0600 -o "$(id -u)" -g "$(id -g)" \
  ./secrets/mcp_token.txt "$HOME/helix-client-transfer/mcp_token.txt"
sudo install -m 0600 -o "$(id -u)" -g "$(id -g)" \
  ./secrets/worker_token.txt "$HOME/helix-client-transfer/worker_token.txt"
install -m 0600 ./exports/caddy-root.crt \
  "$HOME/helix-client-transfer/caddy-root.crt"
sha256sum "$HOME/helix-client-transfer/caddy-root.crt"
```

Windowsで、Codexとworkerを実行するユーザーのPowerShellを開きます。以下の`vmuser`を実際のSSHユーザー名へ変更し、ホスト名も配備したものへ合わせます。

```powershell
$helixLocal = Join-Path $env:LOCALAPPDATA 'HelixMcp'
$helixSecrets = Join-Path $helixLocal 'secrets'
$helixCerts = Join-Path $helixLocal 'certs'
New-Item -ItemType Directory -Force -Path $helixSecrets, $helixCerts | Out-Null

$helixIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls $helixSecrets /inheritance:r /grant:r "${helixIdentity}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F'

scp vmuser@helix.home.arpa:helix-client-transfer/mcp_token.txt "$helixSecrets\mcp_token.txt"
scp vmuser@helix.home.arpa:helix-client-transfer/worker_token.txt "$helixSecrets\worker_token.txt"
scp vmuser@helix.home.arpa:helix-client-transfer/caddy-root.crt "$helixCerts\caddy-root.crt"
Get-FileHash "$helixCerts\caddy-root.crt" -Algorithm SHA256
```

SSHホスト鍵は通常の管理手順で確認してください。CAファイルのSHA-256がVMで確認した値と一致したら、転送が完了です。VM側の転送用tokenコピーは削除できます。`deploy/secrets/`の本体は残します。

```bash
rm -f "$HOME/helix-client-transfer/mcp_token.txt" \
      "$HOME/helix-client-transfer/worker_token.txt"
```

## 2. 内部CAを信頼させる

取得元とハッシュを確認したCaddyのCAだけを、このWindowsユーザーのRootストアへ登録します。[MicrosoftのImport-Certificate資料](https://learn.microsoft.com/en-us/powershell/module/pki/import-certificate)

```powershell
$helixCa = Join-Path $env:LOCALAPPDATA 'HelixMcp\certs\caddy-root.crt'
Import-Certificate -FilePath $helixCa -CertStoreLocation 'Cert:\CurrentUser\Root'
Invoke-RestMethod 'https://helix.home.arpa/healthz'
```

Codex向けにもPEMファイルを明示します。`CODEX_CA_CERTIFICATE`は公式に提供される独自CA設定です。未設定時は`SSL_CERT_FILE`が使われます。[OpenAI公式認証資料](https://learn.chatgpt.com/docs/auth#custom-ca-bundles)

```powershell
$env:CODEX_CA_CERTIFICATE = $helixCa
[Environment]::SetEnvironmentVariable('CODEX_CA_CERTIFICATE', $helixCa, 'User')
```

すでに組織のCA bundleを設定している場合は、既存の証明書を保持したbundleへCaddyのCAを追加して、そのファイルを指定します。既存設定を単独のCaddy CAで置き換えないでください。公開CAの証明書を使用している構成では、この内部CA登録は不要です。

## 3. MCP用tokenを環境変数へ読み込む

```powershell
$helixMcpTokenPath = Join-Path $env:LOCALAPPDATA 'HelixMcp\secrets\mcp_token.txt'
$helixMcpToken = (Get-Content -LiteralPath $helixMcpTokenPath -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($helixMcpToken)) { throw 'MCP token file is empty.' }
$env:HELIX_MCP_TOKEN = $helixMcpToken
[Environment]::SetEnvironmentVariable('HELIX_MCP_TOKEN', $helixMcpToken, 'User')
Remove-Variable helixMcpToken
```

これでtokenはコマンドの引数や`config.toml`に直接書かずに渡せます。ユーザー環境変数はこのWindowsユーザーの設定として保存されます。token値を画面に表示したり、Gitに追加したりする必要はありません。

## 4. Codexへサーバーを登録する

Codex CLIがPATHから利用できる場合は、次を実行します。

```powershell
codex mcp add helix --url https://helix.home.arpa/mcp --bearer-token-env-var HELIX_MCP_TOKEN
codex mcp get helix
```

`--bearer-token-env-var`は、指定した環境変数から認証tokenを読むオプションです。[OpenAI公式CLIリファレンス](https://learn.chatgpt.com/docs/cli/reference)

CLIがPATHにない場合は、既存のユーザー設定`%USERPROFILE%\.codex\config.toml`へ次のテーブルを追加できます。設定ファイル全体を置き換えず、既存の`mcp_servers.helix`があればその項目を編集します。`CODEX_HOME`を独自設定している場合は、その実際の設定ディレクトリを使います。

```toml
[mcp_servers.helix]
url = "https://helix.home.arpa/mcp"
bearer_token_env_var = "HELIX_MCP_TOKEN"
startup_timeout_sec = 20
tool_timeout_sec = 60
```

このサーバーの認証は固定Bearer tokenです。`codex mcp login helix`によるOAuthログインは使用しません。

**Codex Desktopを完全に終了してから起動し直します。** 起動済みプロセスは後から設定した環境変数を自動では受け取りません。スタートメニューから起動したDesktopに設定が届かない場合は、Windowsからサインアウトし、サインインし直してからDesktopを開きます。CLIだけ成功する場合は、Desktopの起動時環境と設定先を確認してください。

## 5. 接続を確認する

`codex mcp get helix`は登録内容の確認であり、ネットワーク接続の検証ではありません。Desktopに次のように依頼してください。

> Helix MCPのservice_statusとworker_statusを呼び、サーバーとWindows workerの状態を確認してください。

まだ[Windows workerの設定](WINDOWS_WORKER.ja.md)を行っていなければ、workerが未接続でもサーバー側の接続確認はできます。

SDK経由で独立して確認したい場合は、Windows workerの手順で作ったリポジトリと`.venv`を使います。

```powershell
# Codex_Helix_Editorのリポジトリ直下で実行する。
$env:HELIX_MCP_TOKEN_FILE = Join-Path $env:LOCALAPPDATA 'HelixMcp\secrets\mcp_token.txt'
$env:SSL_CERT_FILE = Join-Path $env:LOCALAPPDATA 'HelixMcp\certs\caddy-root.crt'
& .\.venv\Scripts\python.exe .\scripts\smoke_mcp.py --url https://helix.home.arpa/mcp
```

このスクリプトは初期化、ツール一覧、読み取り専用の稼働確認を行います。証明書検証は有効のままです。

## 6. プリセット生成を始める

WindowsでこのリポジトリをCodex Desktopのプロジェクトとして開き、実機から書き出した`.hlx`を作業フォルダに用意します。製品プリセットや自分の音色ファイルはGitへコミットしません。

最初の依頼例：

> このHelix Floorから書き出したテンプレートをHelix MCPへ登録してください。登録済みモデルと値を調べ、使える編集範囲を説明してください。

曲の依頼例：

> ○○の「△△」のスタジオ版、2分10秒からのリードギターに寄せた音色を作ってください。登録済みのテンプレートを使い、確認済みのモデルと値だけで候補を生成してください。使用ギターは○○、出力先は○○です。設定済みのWindows宛てに候補を配置してください。

Codexが音色設計を行い、MCPが構造を保った編集とファイル生成を行います。MCP単独で音源を分離・解析したり、未知のHelixモデルを生成したりする機能はありません。テンプレートに必要なブロックがない場合は、実機で用意した別テンプレートが必要です。

現行版の終点はWindows上への配置です。workerから`manual_import_required`と`hardware_applied: false`が返ったら、[手動読み込みと再エクスポート確認](WINDOWS_WORKER.ja.md#手動で読み込んで結果を確認する)を行います。自動GUIインポート完成後も、読込結果を確認できるまでは実機適用済みと扱いません。

## tokenを更新するとき

VM側のMCP tokenを更新してappを再作成した後、Windowsの`mcp_token.txt`を安全な方法で差し替え、この手順の環境変数設定を再実行します。Desktopも再起動します。worker tokenは別の認証なので、MCP tokenの更新だけでは変更しません。

## よくある接続エラー

| 症状 | 次に確認すること |
|---|---|
| MCPサーバーが一覧にない | DesktopとCLIが同じユーザー・同じ`config.toml`を使っているか |
| 401／認証要求 | `HELIX_MCP_TOKEN`がDesktop起動時に存在するか。worker用tokenを入れていないか |
| TLS証明書エラー | CAのハッシュ、`CODEX_CA_CERTIFICATE`の絶対パス、ホスト名の一致 |
| `healthz`は成功するがMCPは失敗 | `/mcp`のURL、Bearer設定、サーバーログ。`healthz`には認証がない |
| 接続できるがworkerがoffline | Windows workerの起動と専用tokenを確認 |
| ツールが拒否される | Codex側の既存の権限設定や管理ポリシーを確認。全体の承認設定を無効化して解決しない |
