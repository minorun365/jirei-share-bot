import { ArnFormat, CfnOutput, Duration, Stack, type StackProps } from "aws-cdk-lib";
import { HttpApi, HttpMethod } from "aws-cdk-lib/aws-apigatewayv2";
import { HttpLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import type { IBedrockAgentRuntime } from "aws-cdk-lib/aws-bedrockagentcore";
import { Alarm, ComparisonOperator, TreatMissingData } from "aws-cdk-lib/aws-cloudwatch";
import { Table } from "aws-cdk-lib/aws-dynamodb";
import { Bucket } from "aws-cdk-lib/aws-s3";
import { PolicyStatement } from "aws-cdk-lib/aws-iam";
import { Function, Runtime } from "aws-cdk-lib/aws-lambda";
import { SqsEventSource } from "aws-cdk-lib/aws-lambda-event-sources";
import { NodejsFunction } from "aws-cdk-lib/aws-lambda-nodejs";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { Queue, QueueEncryption } from "aws-cdk-lib/aws-sqs";
import { Construct } from "constructs";

export interface CaseShareBotIngressStackProps extends StackProps {
  readonly casesTable: Table;
  readonly threadStateTable: Table;
  readonly eventDedupeTable: Table;
  readonly shareLogTable: Table;
  readonly caseAssetsBucket: Bucket;
  readonly agentRuntime: IBedrockAgentRuntime;
  readonly targetChannelId: string;
}

const SLACK_PARAMETER_PREFIX = "/case-share-bot/slack";
const SLACK_SIGNING_SECRET_PARAMETER_NAME = `${SLACK_PARAMETER_PREFIX}/signing-secret`;
const SLACK_BOT_TOKEN_PARAMETER_NAME = `${SLACK_PARAMETER_PREFIX}/bot-token`;

export class CaseShareBotIngressStack extends Stack {
  public readonly slackEventsHandler: Function;

  public constructor(scope: Construct, id: string, props: CaseShareBotIngressStackProps) {
    super(scope, id, props);

    const deadLetterQueue = new Queue(this, "SlackEventDeadLetterQueue", {
      fifo: true,
      encryption: QueueEncryption.SQS_MANAGED,
      retentionPeriod: Duration.days(14)
    });
    const eventQueue = new Queue(this, "SlackEventQueue", {
      fifo: true,
      encryption: QueueEncryption.SQS_MANAGED,
      retentionPeriod: Duration.days(4),
      // Lambda + SQS の推奨値に合わせ、関数タイムアウト（15分）の6倍にする
      visibilityTimeout: Duration.minutes(90),
      deadLetterQueue: {
        queue: deadLetterQueue,
        maxReceiveCount: 3
      }
    });

    const handler = new NodejsFunction(this, "SlackEventsHandler", {
      entry: "src/lambda/slack-events.ts",
      handler: "handler",
      runtime: Runtime.NODEJS_22_X,
      // Slack への ACK は自己 invoke 直後に返す。本処理（AgentCore 呼び出し）は
      // 非同期の自己 invoke 側で走るため、複数事例の一括登録（Drive資料から
      // 6件登録で335秒の実績あり）でも完走できる Lambda 上限の 15 分にする
      timeout: Duration.minutes(15),
      memorySize: 512,
      bundling: {
        minify: true,
        sourceMap: true
      },
      environment: {
        CASES_TABLE_NAME: props.casesTable.tableName,
        THREAD_STATE_TABLE_NAME: props.threadStateTable.tableName,
        EVENT_DEDUPE_TABLE_NAME: props.eventDedupeTable.tableName,
        SHARE_LOG_TABLE_NAME: props.shareLogTable.tableName,
        CASE_ASSETS_BUCKET_NAME: props.caseAssetsBucket.bucketName,
        SLACK_SIGNING_SECRET_PARAMETER_NAME,
        SLACK_BOT_TOKEN_PARAMETER_NAME,
        SLACK_EVENT_QUEUE_URL: eventQueue.queueUrl,
        AGENT_RUNTIME_ARN: props.agentRuntime.agentRuntimeArn,
        TARGET_CHANNEL_ID: props.targetChannelId
      },
      logGroup: new LogGroup(this, "SlackEventsHandlerLogGroup", {
        retention: RetentionDays.ONE_MONTH
      })
    });

    handler.addToRolePolicy(
      new PolicyStatement({
        actions: ["ssm:GetParameter"],
        resources: [
          this.formatArn({
            service: "ssm",
            resource: "parameter",
            resourceName: `${SLACK_PARAMETER_PREFIX.slice(1)}/*`
          })
        ]
      })
    );
    this.slackEventsHandler = handler;
    handler.addToRolePolicy(
      new PolicyStatement({
        actions: ["kms:Decrypt"],
        resources: ["*"],
        conditions: {
          StringEquals: {
            "kms:CallerAccount": this.account,
            "kms:ViaService": `ssm.${this.region}.amazonaws.com`
          }
        }
      })
    );
    props.casesTable.grantReadWriteData(handler);
    props.threadStateTable.grantReadWriteData(handler);
    props.eventDedupeTable.grantReadWriteData(handler);
    props.shareLogTable.grantReadWriteData(handler);
    props.caseAssetsBucket.grantPut(handler, "cases/*");
    props.caseAssetsBucket.grantRead(handler, "cases/*");
    props.agentRuntime.grantInvokeRuntime(handler);
    // grantInvokeRuntime は InvokeAgentRuntime のみ付与する。非対話バッチの完了時に
    // セッションを明示終了してアイドルメモリ課金を止めるため StopRuntimeSession を足す
    handler.addToRolePolicy(
      new PolicyStatement({
        actions: ["bedrock-agentcore:StopRuntimeSession"],
        resources: [
          props.agentRuntime.agentRuntimeArn,
          `${props.agentRuntime.agentRuntimeArn}/*`
        ]
      })
    );
    eventQueue.grantSendMessages(handler);
    handler.addEventSource(
      new SqsEventSource(eventQueue, {
        batchSize: 1
      })
    );
    handler.addToRolePolicy(
      new PolicyStatement({
        actions: ["s3:DeleteObject"],
        resources: [props.caseAssetsBucket.arnForObjects("cases/*")]
      })
    );
    // Slack 3 秒 ACK のための自己 invoke（InvocationType: Event）。
    // handler.functionArn（GetAtt）を自身のロールポリシーに入れると
    // Function -> DefaultPolicy -> Function の循環依存になり、
    // functionName の固定は Lambda 置き換え（＝ScheduleStack が参照する
    // ARN エクスポートの変更）を招くため、CFN 自動命名の
    // 「スタック名-論理ID」プレフィックスへのワイルドカードで許可する
    const selfFunctionArnPattern = this.formatArn({
      service: "lambda",
      resource: "function",
      resourceName: `${this.stackName}-SlackEventsHandler*`,
      arnFormat: ArnFormat.COLON_RESOURCE_NAME
    });
    handler.addToRolePolicy(
      new PolicyStatement({
        actions: ["lambda:InvokeFunction"],
        resources: [selfFunctionArnPattern]
      })
    );
    // 自己 invoke（非同期）のタイムアウト・エラー時に Lambda 既定の 2 回リトライが走ると、
    // 同じ Slack イベントの登録処理が重複実行される（Slack への ACK 済みのため
    // Slack 側の再送は別途 dedupe 済み）。リトライは無効化する
    handler.configureAsyncInvoke({ retryAttempts: 0 });

    new Alarm(this, "SlackEventDeadLetterQueueNotEmptyAlarm", {
      metric: deadLetterQueue.metricApproximateNumberOfMessagesVisible({ period: Duration.minutes(1) }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: TreatMissingData.NOT_BREACHING
    });
    new Alarm(this, "SlackEventQueueOldestMessageAlarm", {
      metric: eventQueue.metricApproximateAgeOfOldestMessage({ period: Duration.minutes(1) }),
      threshold: Duration.minutes(15).toSeconds(),
      evaluationPeriods: 1,
      comparisonOperator: ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: TreatMissingData.NOT_BREACHING
    });

    const api = new HttpApi(this, "SlackEventsApi", {
      apiName: "jirei-share-bot-slack-events"
    });

    api.addRoutes({
      path: "/slack/events",
      methods: [HttpMethod.POST],
      integration: new HttpLambdaIntegration("SlackEventsIntegration", handler)
    });

    new CfnOutput(this, "SlackEventsEndpoint", {
      value: `${api.apiEndpoint}/slack/events`
    });
  }
}
