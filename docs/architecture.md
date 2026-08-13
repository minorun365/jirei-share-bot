# アーキテクチャ

事例共有くんは、Slackの受付、非同期処理、AIエージェント、保存、意味検索、定期実行を分離しています。AWS CDKで5つのCloudFormationスタックを作成します。

| スタック | 主なAWSサービス | 役割 |
|---|---|---|
| State | DynamoDB、S3 | 事例、スレッド状態、重複防止、共有履歴、派生ファイルを保存 |
| Knowledge | Bedrock Knowledge Bases、S3 Vectors | 保存した事例を意味検索 |
| Agent | Bedrock AgentCore Runtime、Strands Agents | 自然文を整理して登録・検索ツールを呼び出す |
| Ingress | API Gateway、Lambda、SQS | Slack署名検証、3秒以内の応答、非同期処理、Slack返信 |
| Schedule | EventBridge Scheduler | 定期共有とナレッジベース同期 |

## Slackから返信まで

1. Slackが `app_mention` イベントをAPI Gatewayへ送信します。
2. LambdaがSigning Secretで署名と時刻を検証します。
3. Lambdaは同じイベントの再送をDynamoDBで判定し、SQSへ処理を渡します。
4. AgentCore Runtime上のエージェントが、登録・更新・検索のいずれかを判断します。
5. エージェントはDynamoDBとS3を更新し、Lambdaが結果をSlackへ返信します。

Slackイベントへの受付と本処理を分けているのは、Slackが要求する短い応答時間を守りながら、資料の読み取りや複数事例の登録を継続するためです。

## データの正本

DynamoDBを事例データの正本とします。S3のMarkdownはKnowledge Basesへ同期する派生データです。検索インデックスだけを更新してDynamoDBを更新しない運用は行いません。

## 削除保護

事例テーブル、S3バケット、Knowledge Base、S3 Vectorsは `RemovalPolicy.RETAIN` です。`cdk destroy` を実行してもデータは残ります。完全に削除する場合は、残ったリソースとデータを確認してから個別に削除してください。
