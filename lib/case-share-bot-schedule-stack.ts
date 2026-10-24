import { Stack, type StackProps } from "aws-cdk-lib";
import { Effect, PolicyStatement, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { IFunction } from "aws-cdk-lib/aws-lambda";
import { CfnSchedule } from "aws-cdk-lib/aws-scheduler";
import { Construct } from "constructs";

export interface CaseShareBotScheduleStackProps extends StackProps {
  readonly dailyShareFunction: IFunction;
  readonly targetChannelId: string;
  readonly dailyShareSchedule: string;
  readonly dailyShareTimezone: string;
  readonly graphicGenerationEnabled: boolean;
  // false なら定期処理をすべて DISABLED で作る（切り替え前の準備や一時停止に使う）
  readonly schedulesEnabled: boolean;
}

export class CaseShareBotScheduleStack extends Stack {
  public constructor(scope: Construct, id: string, props: CaseShareBotScheduleStackProps) {
    super(scope, id, props);

    const scheduleState = props.schedulesEnabled ? "ENABLED" : "DISABLED";

    const schedulerRole = new Role(this, "DailyShareSchedulerRole", {
      assumedBy: new ServicePrincipal("scheduler.amazonaws.com")
    });

    schedulerRole.addToPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["lambda:InvokeFunction"],
        resources: [props.dailyShareFunction.functionArn]
      })
    );

    new CfnSchedule(this, "WeekdayDailyShareSchedule", {
      name: "jirei-share-bot-weekday-daily-share",
      description: "Invoke the case-sharing bot on the configured schedule.",
      flexibleTimeWindow: { mode: "OFF" },
      scheduleExpression: props.dailyShareSchedule,
      scheduleExpressionTimezone: props.dailyShareTimezone,
      state: scheduleState,
      target: {
        arn: props.dailyShareFunction.functionArn,
        roleArn: schedulerRole.roleArn,
        input: Stack.of(this).toJsonString({
          mode: "daily_share",
          channelId: props.targetChannelId
        })
      }
    });

    if (props.graphicGenerationEnabled) {
      new CfnSchedule(this, "PendingGraphicGenerationSchedule", {
        name: "jirei-share-bot-pending-graphic-generation",
        description: "Generate pending case graphics after case updates have been quiet for 5 minutes.",
        flexibleTimeWindow: { mode: "OFF" },
        scheduleExpression: "rate(2 minutes)",
        state: scheduleState,
        target: {
          arn: props.dailyShareFunction.functionArn,
          roleArn: schedulerRole.roleArn,
          input: Stack.of(this).toJsonString({
            mode: "graphic_maintenance"
          })
        }
      });
    }

    new CfnSchedule(this, "KnowledgeBaseSyncSchedule", {
      name: "jirei-share-bot-knowledge-base-sync",
      description: "Regenerate case Markdown and start a Bedrock Knowledge Base ingestion job.",
      flexibleTimeWindow: { mode: "OFF" },
      scheduleExpression: "rate(15 minutes)",
      state: scheduleState,
      target: {
        arn: props.dailyShareFunction.functionArn,
        roleArn: schedulerRole.roleArn,
        input: Stack.of(this).toJsonString({
          mode: "knowledge_base_sync"
        })
      }
    });
  }
}
