# 任意機能

任意機能は初期状態で無効です。必要な機能だけを有効にし、追加する外部送信先とAWS権限を確認してください。

## 公開Web検索

`cdk.json` の `tavilySearchEnabled` を `true` にし、APIキーをParameter Storeへ保存します。

```sh
aws ssm put-parameter \
  --name /case-share-bot/tavily/api-key \
  --type SecureString \
  --value '<TAVILY_API_KEY>' \
  --overwrite
```

検索クエリが外部サービスへ送信されます。社内限定の名称や個人情報を検索語へ入れない運用が必要です。

## Slackメッセージ内のURL読み取り

`cdk.json` の `externalUrlFetchEnabled` を `true` にすると、メッセージやスレッドに含まれるURLを取得し、事例登録の参考情報として使います。HTTP取得に加えて、最終手段としてAgentCore Browserを使うため、その権限も有効になります。

URLの取得先へアクセス元情報が送られます。社内システムや認証付きURLを扱う場合は、取得先、認証方式、保存してよい情報を確認してから有効にしてください。

## Google Drive

`cdk.json` の `googleDriveEnabled` と `externalUrlFetchEnabled` を `true` にします。Google OAuthクライアントと `drive.readonly` の認証情報を用意し、JSONを `/case-share-bot/google/drive-oauth` へ保存します。

この機能は、Botが利用できるOAuth権限でGoogle DriveのURLを読みます。専用アカウントの利用と、共有範囲の定期確認を推奨します。

## グラフィックレコーディング画像

`graphicGenerationEnabled` を `true` にし、Google CloudのProject、Location、画像モデルIDを `cdk.json` へ設定します。Workload Identity Federationの設定は `/case-share-bot/google/wif-credential-config` に保存します。

Slack Appには `files:write` scopeを追加して再インストールします。画像には事例の概要が含まれるため、画像生成サービスへ送信してよい情報だけを対象にしてください。
