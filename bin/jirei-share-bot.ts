#!/usr/bin/env node
import { App, Tags } from "aws-cdk-lib";
import { CaseShareBotAgentStack } from "../lib/case-share-bot-agent-stack.js";
import { CaseShareBotIngressStack } from "../lib/case-share-bot-ingress-stack.js";
import { CaseShareBotKnowledgeStack } from "../lib/case-share-bot-knowledge-stack.js";
import { CaseShareBotScheduleStack } from "../lib/case-share-bot-schedule-stack.js";
import { CaseShareBotStateStack } from "../lib/case-share-bot-state-stack.js";

const app = new App();

const projectTag = app.node.tryGetContext("projectTag") as string | undefined;
Tags.of(app).add("Project", projectTag ?? "jirei-share-bot");

const account = process.env.CDK_DEFAULT_ACCOUNT;
const region = (app.node.tryGetContext("region") as string | undefined) ?? "ap-northeast-1";
const env = account ? { account, region } : { region };

const state = new CaseShareBotStateStack(app, "CaseShareBotStateStack", { env });

const targetChannelId = app.node.tryGetContext("targetChannelId") as string | undefined;
const bedrockModelId = app.node.tryGetContext("bedrockModelId") as string | undefined;

if (!targetChannelId || targetChannelId === "C0123456789") {
  throw new Error("Set context.targetChannelId in cdk.json to your Slack channel ID.");
}
if (!bedrockModelId || bedrockModelId.startsWith("REPLACE_WITH_")) {
  throw new Error("Set context.bedrockModelId in cdk.json to a Bedrock model or inference profile ID.");
}

const knowledge = new CaseShareBotKnowledgeStack(app, "CaseShareBotKnowledgeStack", {
  env,
  caseAssetsBucket: state.caseAssetsBucket
});

const agent = new CaseShareBotAgentStack(app, "CaseShareBotAgentStack", {
  env,
  casesTable: state.casesTable,
  threadStateTable: state.threadStateTable,
  shareLogTable: state.shareLogTable,
  caseAssetsBucket: state.caseAssetsBucket,
  knowledgeBaseId: knowledge.knowledgeBaseId,
  knowledgeBaseArn: knowledge.knowledgeBaseArn,
  knowledgeBaseDataSourceId: knowledge.dataSourceId,
  bedrockModelId,
  bedrockModelDisplayName:
    (app.node.tryGetContext("bedrockModelDisplayName") as string | undefined) ?? "Amazon Bedrock model",
  slackWorkspaceUrl: (app.node.tryGetContext("slackWorkspaceUrl") as string | undefined) ?? "",
  tavilySearchEnabled: app.node.tryGetContext("tavilySearchEnabled") === "true",
  externalUrlFetchEnabled: app.node.tryGetContext("externalUrlFetchEnabled") === "true",
  googleDriveEnabled: app.node.tryGetContext("googleDriveEnabled") === "true",
  graphicGenerationEnabled: app.node.tryGetContext("graphicGenerationEnabled") === "true",
  googleCloudProject: (app.node.tryGetContext("googleCloudProject") as string | undefined) ?? "",
  googleCloudLocation: (app.node.tryGetContext("googleCloudLocation") as string | undefined) ?? "global",
  googleImageModelId: (app.node.tryGetContext("googleImageModelId") as string | undefined) ?? ""
});

const ingress = new CaseShareBotIngressStack(app, "CaseShareBotIngressStack", {
  env,
  casesTable: state.casesTable,
  threadStateTable: state.threadStateTable,
  eventDedupeTable: state.eventDedupeTable,
  shareLogTable: state.shareLogTable,
  caseAssetsBucket: state.caseAssetsBucket,
  agentRuntime: agent.agentRuntime,
  targetChannelId
});

new CaseShareBotScheduleStack(app, "CaseShareBotScheduleStack", {
  env,
  dailyShareFunction: ingress.slackEventsHandler,
  targetChannelId,
  dailyShareSchedule:
    (app.node.tryGetContext("dailyShareSchedule") as string | undefined) ?? "cron(0 12 ? * MON-FRI *)",
  dailyShareTimezone: (app.node.tryGetContext("dailyShareTimezone") as string | undefined) ?? "Asia/Tokyo",
  graphicGenerationEnabled: app.node.tryGetContext("graphicGenerationEnabled") === "true"
});
