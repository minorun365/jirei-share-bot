import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { formatAgentReply, isSnsAlarmEvent, parseAlarmName, withOwnerAlert } from "../src/lambda/slack-events.js";

describe("開発者への失敗通知", () => {
  beforeEach(() => {
    process.env.OWNER_SLACK_USER_ID = "UOWNER0001";
  });
  afterEach(() => {
    delete process.env.OWNER_SLACK_USER_ID;
  });

  it("Runtimeが owner_alert を返したら、返信の末尾で開発者にメンションする", () => {
    const text = formatAgentReply({
      ok: false,
      action: "update",
      slack_text: "更新内容の履歴を保存できなかったため、安全のため更新していません。",
      owner_alert: "case_revision_write_failed"
    });
    expect(text).toContain("<@UOWNER0001>");
    expect(text).toContain("case_revision_write_failed");
    expect(text).not.toContain("時間をおいて");
  });

  it("成功した返信にはメンションを付けない", () => {
    const text = formatAgentReply({ ok: true, action: "register", slack_text: "事例として登録しました。" });
    expect(text).not.toContain("<@UOWNER0001>");
  });

  it("slack_text の無い失敗にもメンションを付ける", () => {
    expect(formatAgentReply({ ok: false, error: "boom" })).toContain("<@UOWNER0001>");
  });

  it("通知先が未設定なら本文だけを返す", () => {
    delete process.env.OWNER_SLACK_USER_ID;
    expect(withOwnerAlert("本文", "x")).toBe("本文");
  });

  it("CloudWatchアラームのSNSイベントを判定し、アラーム名を取り出す", () => {
    const event = {
      Records: [{ EventSource: "aws:sns", Sns: { Message: JSON.stringify({ AlarmName: "SlackEventDeadLetterQueueNotEmptyAlarm" }) } }]
    };
    expect(isSnsAlarmEvent(event)).toBe(true);
    expect(parseAlarmName(event.Records[0]!.Sns.Message)).toBe("SlackEventDeadLetterQueueNotEmptyAlarm");
    expect(isSnsAlarmEvent({ Records: [{ eventSource: "aws:sqs" }] })).toBe(false);
  });
});
