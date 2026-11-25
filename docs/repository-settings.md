# GitHubリポジトリの推奨設定

公開リポジトリを作ったら、最初のリリース前に以下を確認します。

## 必須

- 既定ブランチを `main` にする
- `main` へのforce pushと削除を禁止する
- 変更は必ずPull Request経由にし、CIの成功をマージ条件にする
- [Private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository)を有効にする
- [Secret scanningとpush protection](https://docs.github.com/en/code-security/secret-scanning/introduction/about-secret-scanning)を有効にする
- Dependabot alertsとsecurity updatesを有効にする

## 公開直後

- `.github/ISSUE_TEMPLATE/config.yml` のセキュリティ報告URLを実際のリポジトリURLに変更する
- `SECURITY.md` の案内どおり、非公開で脆弱性を報告できることを確認する
- `v0.1.0` タグとGitHub Releaseを作る
- Topicsに `slack-bot`、`aws-cdk`、`amazon-bedrock`、`agentcore`、`strands-agents` を設定する

## 継続運用

- 依存関係を更新するPull Requestを毎週確認する
- AWS CDKの開発時依存で出る既知の警告は、`SECURITY.md` の記録と照合する
- Slack scopeやAWS権限を増やす変更では、用途とデータフローをPull Requestに記載する
