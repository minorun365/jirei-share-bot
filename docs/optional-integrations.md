# 任意機能

任意機能は初期状態では無効です。必要な機能だけを有効にし、追加される外部送信先とAWS権限を確認してください。

## 公開Web検索

`cdk.json` の `webSearchEnabled` を `true` にします。AgentCore Web Searchを使うため、APIキーを用意してParameter Storeに保存する必要はありません。

有効にすると、Runtimeロールに `bedrock-agentcore:InvokeWebSearch`（`web-search.v1` ツール）の権限が追加されます。検索1回あたりの料金はAWSの利用料に含まれます。

検索クエリはAWSの検索サービスに送信されます。社内限定の名称や個人情報を検索語に入れない運用が必要です。

## Slackメッセージ内のURL読み取り

`cdk.json` の `externalUrlFetchEnabled` を `true` にすると、メッセージやスレッドに含まれるURLを取得し、事例登録の参考情報として使います。HTTPでの取得に加えて、最終手段としてAgentCore Browserも使うため、その権限も有効になります。

URLの取得先にはアクセス元の情報が送られます。社内システムや認証付きURLを扱う場合は、取得先、認証方式、保存してよい情報を確認してから有効にしてください。

## Google Drive

`cdk.json` の `googleDriveEnabled` と `externalUrlFetchEnabled` を `true` にします。Google OAuthクライアントと `drive.readonly` の認証情報を用意し、JSONを `/case-share-bot/google/drive-oauth` に保存します。

この機能は、Botが利用できるOAuth権限でGoogle DriveのURLを読み取ります。専用アカウントを使い、共有範囲を定期的に確認することを推奨します。

## グラフィックレコーディング画像

`graphicGenerationEnabled` を `true` にし、Google CloudのProject、Location、画像モデルIDを `cdk.json` に設定します。Workload Identity Federationの設定は `/case-share-bot/google/wif-credential-config` に保存します。

Slack Appには `files:write` scopeを追加して再インストールします。画像には事例の概要が含まれるため、画像生成サービスに送信してよい情報だけを対象にしてください。
