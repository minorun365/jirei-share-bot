import { App, Stack } from "aws-cdk-lib";
import { Runtime as AgentCoreRuntime } from "aws-cdk-lib/aws-bedrockagentcore";
import { Template } from "aws-cdk-lib/assertions";
import { describe, expect, it } from "vitest";
import { CaseShareBotIngressStack } from "../lib/case-share-bot-ingress-stack.js";
import { CaseShareBotStateStack } from "../lib/case-share-bot-state-stack.js";

describe("CDK logical IDs", () => {
  it("keeps existing stateful resources and Slack ingress resources stable", () => {
    const app = new App();
    const env = { account: "123456789012", region: "us-east-1" };
    const state = new CaseShareBotStateStack(app, "CaseShareBotStateStack", { env });
    const imports = new Stack(app, "ImportedResources", { env });
    const agentRuntime = AgentCoreRuntime.fromAgentRuntimeAttributes(imports, "AgentRuntime", {
      agentRuntimeArn: "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test-runtime",
      agentRuntimeId: "test-runtime",
      agentRuntimeName: "test-runtime",
      roleArn: "arn:aws:iam::123456789012:role/test-agent-runtime-role"
    });
    const ingress = new CaseShareBotIngressStack(app, "CaseShareBotIngressStack", {
      env,
      casesTable: state.casesTable,
      threadStateTable: state.threadStateTable,
      eventDedupeTable: state.eventDedupeTable,
      shareLogTable: state.shareLogTable,
      caseAssetsBucket: state.caseAssetsBucket,
      agentRuntime,
      targetChannelId: "C0123456789"
    });

    const stateResources = Template.fromStack(state).toJSON().Resources as Record<string, unknown>;
    expect(Object.keys(stateResources)).toEqual(
      expect.arrayContaining([
        "CasesTableABF7127D",
        "ThreadStateTableD4FA5FC0",
        "EventDedupeTable6324CC14",
        "ShareLogTable36B03B8C",
        "CaseAssetsBucketADBDC509"
      ])
    );

    const ingressTemplate = Template.fromStack(ingress);
    const ingressResources = ingressTemplate.toJSON().Resources as Record<string, unknown>;
    expect(Object.keys(ingressResources)).toEqual(
      expect.arrayContaining([
        "SlackEventsHandlerLogGroup2F5077AF",
        "SlackEventsHandler5F873887",
        "SlackEventsApi71FAF953"
      ])
    );
    ingressTemplate.resourceCountIs("AWS::SQS::Queue", 2);
    ingressTemplate.hasResourceProperties("AWS::SQS::Queue", {
      FifoQueue: true,
      MessageRetentionPeriod: 345600,
      VisibilityTimeout: 5400,
      RedrivePolicy: {
        maxReceiveCount: 3
      }
    });
    ingressTemplate.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
      BatchSize: 1
    });
    ingressTemplate.resourceCountIs("AWS::CloudWatch::Alarm", 2);
  });
});
