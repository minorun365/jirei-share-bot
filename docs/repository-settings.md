# GitHubリポジトリの推奨設定

公開リポジトリを作成したら、最初のリリース前に次を確認します。

## 必須

- 既定ブランチを `main` にする
- `main` へのforce pushと削除を禁止する
- Pull Request経由の変更を必須にし、CIの成功をマージ条件にする
- [Private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository)を有効にする
- [Secret scanningとpush protection](https://docs.github.com/en/code-security/secret-scanning/introduction/about-secret-scanning)を有効にする
- Dependabot alertsとsecurity updatesを有効にする

## 公開直後

- `.github/ISSUE_TEMPLATE/config.yml` のセキュリティ報告URLを実際のリポジトリURLへ変更する
- `SECURITY.md` の案内どおり、非公開で脆弱性を報告できることを確認する
- `v0.1.0` タグとGitHub Releaseを作る
- Topicsに `slack-bot`、`aws-cdk`、`amazon-bedrock`、`agentcore`、`strands-agents`を設定する

## 継続運用

- 依存更新のPull Requestを週次で確認する
- AWS CDKの開発時依存に関する既知の警告は、`SECURITY.md` の記録と照合する
- Slack scopeやAWS権限を増やす変更では、用途とデータフローをPull Requestへ記載する
