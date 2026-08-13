import { CfnOutput, Duration, Stack, type StackProps } from "aws-cdk-lib";
import {
  AgentRuntimeArtifact,
  ProtocolType,
  Runtime as AgentCoreRuntime,
  RuntimeAuthorizerConfiguration,
  RuntimeNetworkConfiguration
} from "aws-cdk-lib/aws-bedrockagentcore";
import { Platform } from "aws-cdk-lib/aws-ecr-assets";
import { PolicyStatement } from "aws-cdk-lib/aws-iam";
import { Bucket } from "aws-cdk-lib/aws-s3";
import { Table } from "aws-cdk-lib/aws-dynamodb";
import { Construct } from "constructs";
import { CASE_KNOWLEDGE_SOURCE_PREFIX } from "./case-share-bot-knowledge-stack.js";

const GOOGLE_PARAMETER_PREFIX = "/case-share-bot/google";
const GOOGLE_WIF_CREDENTIAL_CONFIG_PARAMETER_NAME = `${GOOGLE_PARAMETER_PREFIX}/wif-credential-config`;

export interface CaseShareBotAgentStackProps extends StackProps {
  readonly casesTable: Table;
  readonly threadStateTable: Table;
  readonly shareLogTable: Table;
  readonly caseAssetsBucket: Bucket;
  readonly knowledgeBaseId: string;
  readonly knowledgeBaseArn: string;
  readonly knowledgeBaseDataSourceId: string;
  readonly bedrockModelId: string;
  readonly bedrockModelDisplayName: string;
  readonly slackWorkspaceUrl: string;
  readonly tavilySearchEnabled: boolean;
  readonly externalUrlFetchEnabled: boolean;
  readonly googleDriveEnabled: boolean;
  readonly graphicGenerationEnabled: boolean;
  readonly googleCloudProject: string;
  readonly googleCloudLocation: string;
  readonly googleImageModelId: string;
}

const TAVILY_PARAMETER_PREFIX = "/case-share-bot/tavily";
const TAVILY_API_KEY_PARAMETER_NAME = `${TAVILY_PARAMETER_PREFIX}/api-key`;
const GOOGLE_DRIVE_OAUTH_PARAMETER_NAME = "/case-share-bot/google/drive-oauth";
export class CaseShareBotAgentStack extends Stack {
  public readonly agentRuntime: AgentCoreRuntime;

  public constructor(scope: Construct, id: string, props: CaseShareBotAgentStackProps) {
    super(scope, id, props);

    const parameterArn = (parameterName: string) =>
      Stack.of(this).formatArn({
        service: "ssm",
        resource: "parameter",
        resourceName: parameterName.replace(/^\//, "")
      });

    const agentRuntime = new AgentCoreRuntime(this, "JireiShareAgentRuntime", {
      runtimeName: "JireiShareBotAgent",
      description: "Strands agent for an internal case-sharing Slack bot.",
      agentRuntimeArtifact: AgentRuntimeArtifact.fromAsset("agentcore-runtime", {
        platform: Platform.LINUX_ARM64
      }),
      protocolConfiguration: ProtocolType.HTTP,
      authorizerConfiguration: RuntimeAuthorizerConfiguration.usingIAM(),
      networkConfiguration: RuntimeNetworkConfiguration.usingPublicNetwork(),
      tracingEnabled: true,
      // メモリ従量課金対策。全 mode が非対話 or DynamoDB で状態を持つステートレス構成のため、
      // アイドルセッションを最短(下限60秒)で終了させて空メモリ課金を抑える。
      // 非対話バッチは Lambda 側で StopRuntimeSession を明示呼びして即終了させ、
      // この 60 秒は停止漏れと対話 follow-up のセッションを終了する安全網として使う。
      lifecycleConfiguration: {
        idleRuntimeSessionTimeout: Duration.seconds(60)
      },
      environmentVariables: {
        CASES_TABLE_NAME: props.casesTable.tableName,
        THREAD_STATE_TABLE_NAME: props.threadStateTable.tableName,
        SHARE_LOG_TABLE_NAME: props.shareLogTable.tableName,
        CASE_ASSETS_BUCKET_NAME: props.caseAssetsBucket.bucketName,
        BEDROCK_REGION: Stack.of(this).region,
        BEDROCK_MODEL_ID: props.bedrockModelId,
        BEDROCK_MODEL_DISPLAY_NAME: props.bedrockModelDisplayName,
        SLACK_WORKSPACE_URL: props.slackWorkspaceUrl,
        CASE_KNOWLEDGE_BASE_ID: props.knowledgeBaseId,
        CASE_KNOWLEDGE_BASE_ARN: props.knowledgeBaseArn,
        CASE_KNOWLEDGE_BASE_DATA_SOURCE_ID: props.knowledgeBaseDataSourceId,
        CASE_KNOWLEDGE_SOURCE_PREFIX,
        CASE_KNOWLEDGE_RETRIEVAL_RESULTS: "5",
        TAVILY_API_KEY_PARAMETER_NAME,
        GOOGLE_DRIVE_OAUTH_PARAMETER_NAME,
        TAVILY_SEARCH_ENABLED: props.tavilySearchEnabled ? "true" : "false",
        EXTERNAL_URL_FETCH_ENABLED: props.externalUrlFetchEnabled ? "true" : "false",
        URL_FETCH_BROWSER_ENABLED: props.externalUrlFetchEnabled ? "true" : "false",
        TAVILY_SEARCH_MAX_RESULTS: "3",
        GOOGLE_DRIVE_ENABLED: props.googleDriveEnabled ? "true" : "false",
        GOOGLE_CLOUD_PROJECT: props.googleCloudProject,
        GOOGLE_CLOUD_LOCATION: props.googleCloudLocation,
        GOOGLE_GENAI_IMAGE_MODEL_ID: props.googleImageModelId,
        GOOGLE_GENAI_USE_VERTEXAI: props.graphicGenerationEnabled ? "true" : "false",
        GOOGLE_WIF_CREDENTIAL_CONFIG_PARAMETER_NAME,
        GEMINI_IMAGE_MODEL: props.googleImageModelId,
        GEMINI_IMAGE_SIZE: "2K",
        GRAPHIC_GENERATION_ENABLED: props.graphicGenerationEnabled ? "true" : "false",
        GRAPHIC_GENERATION_DELAY_SECONDS: "300",
        GRAPHIC_MAINTENANCE_BATCH_SIZE: "1",
        IMAGE_ASSET_PREFIX: "images"
      }
    });

    props.casesTable.grantReadWriteData(agentRuntime);
    props.threadStateTable.grantReadWriteData(agentRuntime);
    props.shareLogTable.grantReadWriteData(agentRuntime);
    props.caseAssetsBucket.grantReadWrite(agentRuntime, "cases/*");
    props.caseAssetsBucket.grantReadWrite(agentRuntime, `${CASE_KNOWLEDGE_SOURCE_PREFIX}*`);
    props.caseAssetsBucket.grantReadWrite(agentRuntime, "images/*");
    agentRuntime.addToRolePolicy(
      new PolicyStatement({
        actions: ["s3:DeleteObject"],
        resources: [
          props.caseAssetsBucket.arnForObjects("cases/*"),
          props.caseAssetsBucket.arnForObjects(`${CASE_KNOWLEDGE_SOURCE_PREFIX}*`),
          props.caseAssetsBucket.arnForObjects("images/*")
        ]
      })
    );
    const optionalParameterNames = [
      ...(props.tavilySearchEnabled ? [TAVILY_API_KEY_PARAMETER_NAME] : []),
      ...(props.googleDriveEnabled ? [GOOGLE_DRIVE_OAUTH_PARAMETER_NAME] : []),
      ...(props.graphicGenerationEnabled ? [GOOGLE_WIF_CREDENTIAL_CONFIG_PARAMETER_NAME] : [])
    ];
    if (optionalParameterNames.length > 0) {
      agentRuntime.addToRolePolicy(
        new PolicyStatement({
          actions: ["ssm:GetParameter"],
          resources: optionalParameterNames.map(parameterArn)
        })
      );
      agentRuntime.addToRolePolicy(
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
    }
    agentRuntime.addToRolePolicy(
      new PolicyStatement({
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:GetInferenceProfile"],
        resources: [
          Stack.of(this).formatArn({
            service: "bedrock",
            resource: "foundation-model",
            resourceName: "*",
            account: "",
            region: "*"
          }),
          Stack.of(this).formatArn({
            service: "bedrock",
            resource: "inference-profile",
            resourceName: "*",
            region: "*"
          }),
          Stack.of(this).formatArn({
            service: "bedrock",
            resource: "application-inference-profile",
            resourceName: "*",
            region: "*"
          })
        ]
      })
    );
    agentRuntime.addToRolePolicy(
      new PolicyStatement({
        actions: ["bedrock:Retrieve", "bedrock:GetKnowledgeBase"],
        resources: [props.knowledgeBaseArn]
      })
    );
    if (props.externalUrlFetchEnabled) {
      // URL読み取りの最終フォールバックで使うAgentCore Browser。
      agentRuntime.addToRolePolicy(
        new PolicyStatement({
          actions: ["bedrock-agentcore:*Browser*"],
          resources: [
            `arn:aws:bedrock-agentcore:${this.region}:aws:browser/aws.browser.v1`,
            `arn:aws:bedrock-agentcore:${this.region}:aws:browser/aws.browser.v1/*`,
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:browser/aws.browser.v1`,
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:browser/aws.browser.v1/*`
          ]
        })
      );
    }
    agentRuntime.addToRolePolicy(
      new PolicyStatement({
        actions: ["bedrock:StartIngestionJob", "bedrock:GetIngestionJob", "bedrock:ListIngestionJobs"],
        resources: [
          props.knowledgeBaseArn,
          `${props.knowledgeBaseArn}/data-source/${props.knowledgeBaseDataSourceId}`
        ]
      })
    );

    this.agentRuntime = agentRuntime;

    new CfnOutput(this, "AgentRuntimeArn", {
      value: agentRuntime.agentRuntimeArn
    });
    new CfnOutput(this, "AgentRuntimeId", {
      value: agentRuntime.agentRuntimeId
    });
  }
}
