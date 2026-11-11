# 曲の指示から候補プリセットまで

## 初回に用意するもの

1. 自分のHelix FloorからHX EditでExportした`.hlx`。まず一つのリード用プリセットで開始。
2. 登録先のセットリスト番号1〜8とスロット01A〜32D。名前も設定すると確認しやすくなります。
3. ギター／ピックアップ、出力先、最初のアーティスト・曲・区間。

ExportはCodexが読み取れるWindowsの作業フォルダへ置きます。MCPの`register_template`にはそのUTF-8 JSON文字列を渡します。VM上の任意パスを指定して読み込ませるAPIはありません。ファイルはGitに追加しません。

## Codexへ渡す依頼例

> Helix MCPの状態を確認して。このフォルダにある実機Exportをテンプレートとして登録し、○○の△△、2:10からのリード音の候補を作って。ギターは□□、出力はヘッドホン。作った候補をWindowsワーカーへ送って。

## 処理順

1. `service_status`と`worker_status`を確認。
2. `register_template`、`inspect_template`で実データを調査。
3. 曲の資料・利用可能な音源から再現方針を作る。確認済み情報と推定を分ける。
4. `create_edit_plan`で空の変更計画を取得し、Codexが`edits`を埋める。
5. `build_candidate`で別の候補を保存。`get_candidate`で変更内容を確認。
6. `stage_preset_for_windows`へ候補IDと一意な`request_id`を渡す。同じ依頼の通信再試行は同じrequest_idを使う。
7. `get_job`を読み、`manual_import_required`とWindows上の保存パスを確認。
8. WindowsのHX Editで対象を確認し、既存内容をバックアップしてから手動Import。
9. 再ExportしたJSONを`compare_readback`へ渡してデータを照合し、試奏で調整。

## 変更計画の形式

`create_edit_plan`の戻り値をそのまま使い、`template_sha256`を維持します。各editには以下が必要です。

| キー | 内容 |
| --- | --- |
| `path` | inspectで実際に現れたJSON Pointer |
| `expect` | そのテンプレートの元の値 |
| `value` | 変更後の値 |
| `model` | 元のブロックの`@model` |
| `kind` | `number`、`integer`、`boolean` |
| `min` / `max` | 数値の場合、確認済みの生値の範囲 |
| `evidence` | 型・範囲・表示値との対応を確認した根拠 |

数値をすべて0〜1と仮定しません。必要ならHX Edit上で一つのパラメータを変えて再Exportし、差分から対応を確認します。evidence欄の内容自体をプログラムが証明するわけではありません。

現在の生成器は、既存ブロックの直接の数値／boolパラメータだけを変更します。モデル変更、ブロック追加、`@enabled`、入出力ルーティングの変更は含みません。元が整数なら整数の編集だけを許可します。Snapshot／Controllerに非対応構造がある場合も停止するため、そのテンプレートの調査または単純なテンプレートへの変更が必要です。

## 完了の意味

| 表示 | 意味 |
| --- | --- |
| `candidate_not_imported` | 候補ファイルを作成。実機登録は未実施 |
| job `queued` | Windowsがまだ受け取っていない |
| job `leased` | Windowsへ渡した。結果待ち |
| job `succeeded` + `manual_import_required` | Windowsへファイルを配置。手動Importが必要 |
| job `unknown` | 結果が不明。Windowsのjournalと状態を調べてから判断 |
| `tone_data_matches=true` | 再Exportされた音色データが一致。保存スロットや音質は別確認 |

## 自動Import adapterを追加する条件

実際のWindows上で、Helix Floorのウィンドウ、Presetsパネル、対象セットリスト、対象スロット、Export／Importダイアログを識別します。バックアップ取得→対象選択→Import→登録先確認→再Exportの一件を検証してから、自動適用機能を公開します。

Linux VMのMCPを起動することと、Windows GUIの自動操作を検証することは別の工程です。UIA診断だけではUSB接続や実機適用を証明できません。

## 形式の参考資料

- [Line 6 HX Editマニュアル](https://line6.com/support/manuals/hxedit/)：プリセットの読み込み・書き出し・互換性。
- [tonepush作者の調査メモ](https://github.com/crmne/tonepush/blob/main/docs/_reference/model-catalog.md) と [MatchPatch作者の形式説明](https://github.com/noseglasses/MatchPatch/blob/develop/docs/dev/file-formats.md)：.hlx構造の参考。Line 6公式スキーマではありません。
