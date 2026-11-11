# Proxmox VE上のLinux VMへの配備

この手順は、既存のProxmox VE（PVE）に新しいLinux VMを作り、Helix MCPサーバーをDocker Composeで動かすためのものです。Helix FloorはWindows PCにUSB接続したまま使います。Linux VMへのUSBパススルーは使用しません。

## この版の到達点

サーバーはテンプレート保存、既存パラメータの編集、候補プリセット生成、Windowsへのジョブ配送を担当します。Windows workerはHX Editの検出・起動・診断と候補ファイルの配置までを行います。**HX Editの指定スロットへ自動インポートするGUI処理は未実装です。** `manual_import_required` は手動読み込み待ちを意味し、実機への適用成功ではありません。

| 接続 | 用途 | 認証 |
|---|---|---|
| Windows Codex → VMの`/mcp` | Streamable HTTPのMCPツール | MCP専用Bearer token |
| Windows worker → VMの`/worker/*` | ジョブ取得・結果報告 | worker専用Bearer token |
| クライアント → VMの`/healthz` | 最小限の稼働確認 | なし |
| Caddy → app:8080 | Docker内の転送 | 外部へ8080番を公開しない |

既定ではLAN内の名前`helix.home.arpa`とCaddyの内部CAを使います。外部サービスの契約、公開DNS、インターネットからの受信は不要です。Caddyの内部CAをクライアントに信頼させる作業は必要です。[Caddy公式資料](https://caddyserver.com/docs/automatic-https)

## 1. VMと名前を用意する

以下はこの小規模サービス向けの初期構成案であり、測定済みの最低要件ではありません。

| 項目 | 初期値の例 |
|---|---|
| OS | Debian 12、またはUbuntu 24.04 LTS |
| CPU | 2 vCPU |
| メモリー | 2 GiB |
| ディスク | 20 GiB以上。保存数・バックアップ数に応じて追加 |
| ネットワーク | Windows PCから到達できるLAN。固定IPまたはDHCP予約 |
| 公開ポート | TCP 443。SSH管理用TCP 22は管理元のみ |

PVEのWeb UIでISOをアップロードし、**Create VM**を開きます。VM ID・ISOを置くストレージ・ディスクを置くストレージ・ブリッジは、実際に存在するものを画面で選びます。特定のストレージIDや`vmbr`名を前提にしたコマンドは使用しません。OSをインストールし、SSHでログインできる管理ユーザーを用意してください。必要ならQEMU Guest AgentをインストールしてPVE側でも有効化します。[Proxmox VE公式管理ガイド](https://pve.proxmox.com/pve-docs/pve-admin-guide.pdf)

LANのDNSに、使用する名前からVMのIPへのレコードを登録します。DNSを変更できない場合は、Windowsの`C:\Windows\System32\drivers\etc\hosts`に管理者権限で同じ対応を追加する方法でも構いません。以下の例に出てくるIP・ユーザー名は自分の値に置き換えます。

```text
192.168.1.50 helix.home.arpa
```

PVE/ネットワークのファイアウォールでは、Windows PCまたは必要なLANからVMのTCP 443への接続を許可します。Dockerで公開したポートはホストのUFW設定だけでは意図どおり制限できない場合があるため、PVE側や上流側でも到達範囲を設定してください。[Docker公式資料](https://docs.docker.com/engine/install/debian/#firewall-limitations)

## 2. VMにDocker EngineとComposeを導入する

この手順のDocker daemonは標準のDocker Engineです。アプリコンテナ内はUID/GID `10001`で実行します。Docker daemonそのものをrootlessモードにする手順ではありません。

新規のDebian/Ubuntu VMで、まず必要なコマンドを入れます。

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git openssl
```

すでにDocker関連パッケージがあるVMでは、公式の競合パッケージ確認を先に行います。既存のコンテナ環境を一括削除する操作はこの手順に含めません。

Docker公式APTリポジトリを設定します。次の例はDebianとUbuntuを判別します。

```bash
. /etc/os-release
case "$ID" in
  debian|ubuntu) docker_os="$ID" ;;
  *) echo 'Use the Docker installation guide for this OS.' >&2; exit 1 ;;
esac
docker_codename="${UBUNTU_CODENAME:-$VERSION_CODENAME}"
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL "https://download.docker.com/linux/$docker_os/gpg" \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/$docker_os
Suites: $docker_codename
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker compose version
sudo docker run --rm hello-world
```

ディストリビューションのサポート範囲と更新方法は、[Debian用の公式手順](https://docs.docker.com/engine/install/debian/)または[Ubuntu用の公式手順](https://docs.docker.com/engine/install/ubuntu/)を参照してください。

## 3. コードと設定を配置する

以下は管理ユーザーのホームディレクトリに配置する例です。非公開リポジトリの場合は、自分の通常のGitHub認証で取得してください。認証トークンをURLに埋め込まないでください。

```bash
git clone https://github.com/Ron3Ly/Codex_Helix_Editor.git
cd Codex_Helix_Editor/deploy
cp .env.example .env
nano .env
```

最初に設定する値は次のとおりです。

```dotenv
HELIX_DOMAIN=helix.home.arpa
HELIX_BIND_IP=0.0.0.0
HELIX_WORKER_ID=helix-floor-windows
```

`HELIX_DOMAIN`はプロトコル、パス、ポートを付けないホスト名です。`HELIX_BIND_IP=0.0.0.0`はVMの全IPv4インターフェースで443番を受けます。特定のLANアドレスだけで受ける場合は、実際のVMのLAN IPへ変更します。

配置先は実機のHX Editで確認してから設定します。未設定でもサーバーの稼働確認、テンプレート管理、候補生成は使用できますが、Windowsへの配置要求は拒否されます。

```dotenv
# 例。使用中のスロットを確認してから、自分の配置先へ変更する。
HELIX_TARGET_SETLIST=8
HELIX_TARGET_SLOT=01A
HELIX_TARGET_SETLIST_NAME=AI Tones
```

上記はセットリストやスロットを新規作成・初期化する設定ではありません。`HELIX_TARGET_SETLIST`は1～8、`HELIX_TARGET_SLOT`は`01A`～`32D`です。名前は人が確認するためのラベルです。

`HELIX_ALLOWED_ORIGINS`は通常空のまま使います。この場合、Originヘッダーを持つ要求は拒否します。ネイティブクライアントで必要が判明した場合のみ、送信元の正確なOriginを設定してください。ワイルドカードを使って許可を広げないでください。

## 4. 保存先と認証情報を初期化する

```bash
sudo bash ./prepare-vm-storage.sh
```

このスクリプトは次のものを作成します。既存のトークン値を置き換えません。

| 場所 | 内容・権限 |
|---|---|
| `state/data/` | サーバーDB用。UID/GID 10001、モード0700 |
| `secrets/mcp_token.txt` | Codex用。root:10001、モード0440 |
| `secrets/worker_token.txt` | Windows worker用。root:10001、モード0440 |

トークンは256ビットの乱数からローカル生成し、値を標準出力に表示しません。Composeはそれぞれを`/run/secrets/`にマウントします。Docker Composeのファイル型secretはbind mountなので、ホスト側の所有者・読み取り権限を初期化しています。[Docker Compose公式資料](https://docs.docker.com/compose/how-tos/use-secrets/)

`.env`、`secrets/`、`state/`、`backups/`、`exports/`はGitに含めません。トークンをチャット、Issue、設定例へ貼り付ける必要はありません。

## 5. ビルドして起動する

```bash
sudo docker compose config --quiet
sudo docker compose build app
sudo docker compose run --rm --no-deps app helix-mcp check-config
sudo docker compose up -d --wait --wait-timeout 60
sudo docker compose ps
sudo docker compose logs --tail=80 app caddy
```

`check-config`はトークン値を表示せず、必要なファイルと設定を検証します。appがhealthyになってからCaddyが起動します。アプリの8080番はホストへ公開されず、Caddyの443番だけが公開されます。

サーバー内部でMCPの初期化、ツール一覧、読み取り専用の稼働確認を実行します。

```bash
sudo docker compose exec -T app python - \
  --url http://127.0.0.1:8080/mcp < ../scripts/smoke_mcp.py
```

この確認はコンテナ内のPythonとSDKを使うため、Debian 12ホストに別途Python 3.12を入れる必要はありません。外部のTLS/DNSは次の手順で確認します。

## 6. CaddyのCA証明書を取り出し、HTTPSを確認する

初回起動後、公開部分のルート証明書だけを取り出します。

```bash
mkdir -p exports
sudo docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt \
  ./exports/caddy-root.crt
sudo chown "$(id -u):$(id -g)" ./exports/caddy-root.crt
openssl x509 -in ./exports/caddy-root.crt -noout -subject -fingerprint -sha256
sha256sum ./exports/caddy-root.crt
```

表示したフィンガープリントを控えます。Windowsへ渡すのは`caddy-root.crt`です。`root.key`などのCA秘密鍵はクライアントへ渡しません。Caddyの内部CA証明書と更新状態は`caddy_data`ボリュームに保存されるため、このボリュームを不用意に作り直さないでください。[CaddyのLocal HTTPS](https://caddyserver.com/docs/automatic-https#local-https)

VM上で、実際に設定したホスト名を使って確認します。VMのDNSで同名を解決できない場合は、`--resolve`で今回だけloopbackに向けます。

```bash
curl --fail --cacert ./exports/caddy-root.crt \
  --resolve helix.home.arpa:443:127.0.0.1 \
  https://helix.home.arpa/healthz

# 認証なしのMCP要求が401になることを確認する。
curl --silent --output /dev/null --write-out '%{http_code}\n' \
  --cacert ./exports/caddy-root.crt \
  --resolve helix.home.arpa:443:127.0.0.1 \
  https://helix.home.arpa/mcp
```

`HELIX_BIND_IP`をLAN IPに限定した場合は、`--resolve`の末尾もそのIPへ変更します。ホスト名を変更した場合はURLと`--resolve`の両方を変更します。`curl -k`で検証を省略せず、名前またはCA設定を修正します。

Windowsへの証明書・トークン転送と登録は、[Codex接続](CODEX_SETUP.ja.md)と[Windows worker](WINDOWS_WORKER.ja.md)を続けて実施してください。

## 7. データのバックアップ

中央のテンプレート、候補プリセット、ジョブは`state/data/helix.sqlite3`に保存されます。稼働中のSQLiteファイルを単純コピーせず、実装済みのオンラインバックアップを使います。

```bash
backup_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
sudo docker compose exec -T app helix-mcp backup \
  --output "/data/backups/helix-${backup_stamp}.sqlite3"
sudo install -d -m 0700 ./backups
sudo cp "./state/data/backups/helix-${backup_stamp}.sqlite3" ./backups/
```

バックアップの作成日時はUTCで名前に入れています。保存済みのDBバックアップをVM外へコピーし、PVEのVMバックアップも別途設定します。VMの同じディスク内だけのコピーでは、そのディスクの故障には備えられません。

復旧に必要なものは以下です。

- DBのオンラインバックアップ。
- `.env`と、同じ配置で使用する二つのtokenファイル。
- Caddyの`/data`と`/config`。CA秘密鍵を含むため、アクセスを限定したバックアップ先へ保存。
- 配備したGitコミットIDと使用イメージ。
- Windows workerのデータフォルダ（spool、journal、診断）。これはVMのDBとは別です。

Caddyの保存領域をファイルとして取る場合は、短時間停止してコピーできます。

```bash
sudo docker compose stop caddy
sudo docker compose cp caddy:/data "./backups/caddy-data-${backup_stamp}"
sudo docker compose cp caddy:/config "./backups/caddy-config-${backup_stamp}"
sudo docker compose start caddy
git rev-parse HEAD
sudo docker compose images
```

## 8. 更新とロールバック

更新前にDBバックアップと現在のコミットIDを記録します。Windows workerは一度停止し、新しいジョブが増えない状態にします。起動中のアプリイメージにも別タグを付けて残せます。

```bash
rollback_tag="helix-mcp:rollback-$(date -u +%Y%m%dT%H%M%SZ)"
sudo docker image tag "$(sudo docker compose images -q app)" "$rollback_tag"
git status --short
git fetch origin
# 確認したリリースタグまたはコミットIDへ切り替える。
git checkout --detach REPLACE_WITH_REVIEWED_TAG_OR_COMMIT
sudo docker compose build --pull app
sudo docker compose run --rm --no-deps app helix-mcp check-config
sudo docker compose up -d --wait --wait-timeout 60
sudo docker compose ps
sudo docker compose exec -T app python - \
  --url http://127.0.0.1:8080/mcp < ../scripts/smoke_mcp.py
```

`REPLACE_WITH_REVIEWED_TAG_OR_COMMIT`は実際の値へ置き換えます。Pythonのベースイメージは3.12系列、Caddyは2系列のタグなので、完全に同じイメージを再利用したい運用では、テスト済みのdigestへ固定してください。依存関係を更新する場合も、この確認を再実施します。

アプリだけを戻す場合は、対応する旧コミットへ戻し、`.env`の`HELIX_IMAGE`を記録したrollbackタグへ変更してから起動します。

```bash
sudo docker compose up -d --no-build --force-recreate --wait --wait-timeout 60 app
```

DB形式も戻す必要がある更新では、workerとappを停止し、現在のDB・`-wal`・`-shm`を別フォルダへ退避してから対応するバックアップを`state/data/helix.sqlite3`として復元します。復元したDBは所有者10001:10001、モード0600に戻します。旧DBと現在のWALを混在させないでください。再起動後はジョブの状態を確認してからworkerを再開します。

DBを復元する具体例です。最初のパスを実在するバックアップへ変更し、先にWindows workerを停止してください。

```bash
restore_from='./backups/REPLACE_WITH_BACKUP_FILENAME.sqlite3'
sudo test -f "$restore_from" || exit 1
restore_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
sudo docker compose stop app
sudo install -d -m 0700 "./backups/before-restore-${restore_stamp}"
for db_suffix in '' '-wal' '-shm'; do
  if sudo test -e "./state/data/helix.sqlite3${db_suffix}"; then
    sudo mv "./state/data/helix.sqlite3${db_suffix}" \
      "./backups/before-restore-${restore_stamp}/"
  fi
done
sudo install -m 0600 -o 10001 -g 10001 "$restore_from" ./state/data/helix.sqlite3
sudo docker compose up -d --wait --wait-timeout 60 app
sudo docker compose ps
sudo docker compose exec -T app python - \
  --url http://127.0.0.1:8080/mcp < ../scripts/smoke_mcp.py
```

新品のVMへ全体を復旧する場合は、同じリリース・設定・tokenを配置して権限を整え、起動前にDBを戻します。Caddyは`docker compose create`でコンテナと空の保存ボリュームを作ってから、退避した`/data`と`/config`の内容を`docker compose cp`で同じ場所へ戻し、その後`up -d`します。既存の別CAの保存領域に混ぜる操作は行わず、新規ボリュームへ復元します。PVEのVMバックアップを使う復旧では、これらの保存領域を含むVM全体を戻せます。

## 9. 公開DNSの証明書を使う場合だけ

既定の内部CA方式で問題なければ、この節は不要です。自分の公開ドメインと到達経路がある場合は、`HELIX_DOMAIN`をそのホスト名へ変更し、`Caddyfile`の`tls internal`を削除すると、Caddyの公開CAによる証明書取得を使えます。HTTP challengeも使う構成では80番をCaddyへ追加公開し、DNS・NAT・ファイアウォールを整えます。DNS challengeを使う場合は対応プラグイン等の別設定が必要です。[CaddyのACME手順](https://caddyserver.com/docs/automatic-https#acme-challenges)

公開証明書を選んでも、MCPとworkerのBearer認証は維持します。公開しない運用ではルーターのインターネット側ポート転送は作成しません。

## トラブルシューティング

| 症状 | 確認する場所 |
|---|---|
| appが起動しない／unhealthy | `docker compose logs app`、`check-config`、tokenの所有者・権限、`state/data`のUID10001 |
| HTTPSへ接続できない | VMのIP・DNS・`HELIX_BIND_IP`・PVE/上流FWの443番、Caddyログ |
| 証明書エラー | URLの名前と`HELIX_DOMAIN`が一致するか、正しいCaddy CAを信頼したか |
| 401 | MCPとworkerのtokenを取り違えていないか、ファイル末尾以外の余計な文字がないか |
| 403／不正なHost・Origin | Hostを`HELIX_DOMAIN`と一致させる。Originは必要な値だけ追加 |
| workerがoffline | Windowsが起動・ログイン中か、workerの環境変数・CA・専用token、worker IDの一致 |
| stagingが拒否される | `.env`のセットリスト／スロットが未設定でないか |
| `manual_import_required` | 正常な現行仕様。Windows上の候補ファイルをHX Editから手動Importする |
| `No device connected` | Helixの電源・USB・Windows側HX Editを確認。VMのUSB設定は変更しない |

実配備時には、上記の確認結果と使ったバージョンを記録してください。このリポジトリのテストは、個別のPVEネットワーク、Windows GUI、Helix Floor実機での動作を証明するものではありません。
