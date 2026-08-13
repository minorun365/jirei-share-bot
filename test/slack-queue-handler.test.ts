import { createHmac } from "node:crypto";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  agentSend: vi.fn(),
  dynamodbSend: vi.fn(),
  sqsSend: vi.fn(),
  ssmSend: vi.fn()
}));

vi.mock("@aws-sdk/client-bedrock-agentcore", () => ({
  BedrockAgentCoreClient: class {
    public send(command: unknown): unknown {
      return mocks.agentSend(command);
    }
  },
  InvokeAgentRuntimeCommand: class {
    public constructor(public readonly input: Record<string, unknown>) {}
  }
}));

vi.mock("@aws-sdk/client-sqs", () => ({
  SQSClient: class {
    public send(command: unknown): unknown {
      return mocks.sqsSend(command);
    }
  },
  SendMessageCommand: class {
    public constructor(public readonly input: Record<string, unknown>) {}
  }
}));

vi.mock("@aws-sdk/client-ssm", () => ({
  SSMClient: class {
    public send(command: unknown): unknown {
      return mocks.ssmSend(command);
    }
  },
  GetParameterCommand: class {
    public constructor(public readonly input: Record<string, unknown>) {}
  }
}));

vi.mock("@aws-sdk/lib-dynamodb", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@aws-sdk/lib-dynamodb")>();
  return {
    ...actual,
    DynamoDBDocumentClient: {
      from: () => ({ send: mocks.dynamodbSend })
    }
  };
});

import { buildSlackQueueMessage, handler } from "../src/lambda/slack-events.js";

const SIGNING_SECRET = "test-signing-secret";

describe("Slack queue handler", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.assign(process.env, {
      AGENT_RUNTIME_ARN: "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
      EVENT_DEDUPE_TABLE_NAME: "event-dedupe",
      SLACK_BOT_TOKEN_PARAMETER_NAME: "/test/slack/bot-token",
      SLACK_EVENT_QUEUE_URL: "https://sqs.us-east-1.amazonaws.com/123456789012/slack-events.fifo",
      SLACK_SIGNING_SECRET_PARAMETER_NAME: "/test/slack/signing-secret"
    });
    mocks.sqsSend.mockResolvedValue({});
    mocks.ssmSend.mockImplementation(async (command: { readonly input: { readonly Name?: string } }) => ({
      Parameter: {
        Value: command.input.Name?.includes("signing-secret") ? SIGNING_SECRET : "xoxb-test"
      }
    }));
    mocks.dynamodbSend.mockImplementation(async (command: { readonly constructor: { readonly name: string } }) =>
      command.constructor.name === "GetCommand" ? {} : {}
    );
    mocks.agentSend.mockResolvedValue(agentResponse("処理しました"));
    vi.stubGlobal("fetch", vi.fn(slackFetch));
  });

  it("acknowledges only after the event has been queued", async () => {
    const body = JSON.stringify(appMentionPayload("Ev-http", "C-http"));
    const timestamp = String(Math.floor(Date.now() / 1000));
    const response = await handler({
      body,
      headers: {
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": sign(timestamp, body)
      },
      isBase64Encoded: false
    } as never);

    expect(response).toMatchObject({ statusCode: 200 });
    expect(mocks.sqsSend).toHaveBeenCalledTimes(1);
    expect(mocks.dynamodbSend).not.toHaveBeenCalled();
  });

  it("does not acknowledge when queue persistence fails", async () => {
    mocks.sqsSend.mockRejectedValueOnce(new Error("SQS unavailable"));
    const body = JSON.stringify(appMentionPayload("Ev-sqs-failure", "C-sqs-failure"));
    const timestamp = String(Math.floor(Date.now() / 1000));

    await expect(
      handler({
        body,
        headers: {
          "x-slack-request-timestamp": timestamp,
          "x-slack-signature": sign(timestamp, body)
        },
        isBase64Encoded: false
      } as never)
    ).rejects.toThrow("SQS unavailable");
  });

  it("stores the AgentCore response before posting and marks delivery", async () => {
    await handler(sqsEvent("Ev-worker", "C-worker", "1") as never);

    expect(mocks.agentSend).toHaveBeenCalledTimes(1);
    expect(slackMethods()).toEqual(["conversations.history", "chat.postMessage"]);
    expect(dynamodbStatuses()).toEqual(["agent_completed", "delivered"]);
  });

  it("reuses a stored AgentCore response on a delivery retry", async () => {
    mocks.dynamodbSend.mockImplementation(async (command: { readonly constructor: { readonly name: string } }) =>
      command.constructor.name === "GetCommand"
        ? {
            Item: {
              event_id: "Ev-cached",
              status: "agent_completed",
              agent_response: { ok: true, action: "search", slack_text: "保存済み応答" }
            }
          }
        : {}
    );

    await handler(sqsEvent("Ev-cached", "C-cached", "2") as never);

    expect(mocks.agentSend).not.toHaveBeenCalled();
    expect(slackMethods()).toEqual(["conversations.history", "chat.postMessage"]);
    expect(dynamodbStatuses()).toContain("delivered");
  });

  it("skips an event that has already been delivered", async () => {
    mocks.dynamodbSend.mockResolvedValueOnce({ Item: { event_id: "Ev-delivered", status: "delivered" } });

    await handler(sqsEvent("Ev-delivered", "C-delivered", "2") as never);

    expect(mocks.agentSend).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
    expect(mocks.dynamodbSend).toHaveBeenCalledTimes(1);
  });

  it("notifies once on the final receive and leaves the message for the DLQ", async () => {
    mocks.agentSend.mockRejectedValueOnce(new Error("AgentCore unavailable"));

    await expect(handler(sqsEvent("Ev-final", "C-final", "3") as never)).rejects.toThrow(
      "AgentCore unavailable"
    );

    expect(slackMethods()).toEqual(["conversations.history", "chat.postMessage"]);
    expect(dynamodbStatuses()).toContain("failed_notified");
  });

  it("keeps scheduled maintenance invocations off the Slack event queue", async () => {
    const result = await handler({ mode: "graphic_maintenance" });

    expect(result).toMatchObject({ ok: true });
    expect(mocks.agentSend).toHaveBeenCalledTimes(1);
    expect(mocks.sqsSend).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });
});

function appMentionPayload(eventId: string, channel: string) {
  return {
    type: "event_callback" as const,
    event_id: eventId,
    event: {
      type: "app_mention",
      channel,
      ts: "1784109763.000001",
      text: "事例を検索して"
    }
  };
}

function sqsEvent(eventId: string, channel: string, receiveCount: string) {
  const message = buildSlackQueueMessage(appMentionPayload(eventId, channel));
  return {
    Records: [
      {
        body: message.body,
        eventSource: "aws:sqs",
        attributes: { ApproximateReceiveCount: receiveCount }
      }
    ]
  };
}

function agentResponse(slackText: string) {
  return {
    response: {
      transformToString: async () =>
        JSON.stringify({ ok: true, action: "search", slack_text: slackText })
    }
  };
}

async function slackFetch(input: string | URL | Request): Promise<Response> {
  const method = String(input).split("/").at(-1);
  const payload = method === "chat.postMessage" ? { ok: true, ts: "1784109764.000001" } : { ok: true };
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" }
  });
}

function slackMethods(): string[] {
  return vi.mocked(fetch).mock.calls.map(([input]) => String(input).split("/").at(-1) ?? "");
}

function dynamodbStatuses(): string[] {
  return mocks.dynamodbSend.mock.calls.flatMap(([command]) => {
    const input = (command as { readonly input?: { readonly ExpressionAttributeValues?: Record<string, unknown> } }).input;
    const status = input?.ExpressionAttributeValues?.[":status"];
    return typeof status === "string" ? [status] : [];
  });
}

function sign(timestamp: string, body: string): string {
  const base = `v0:${timestamp}:${body}`;
  return `v0=${createHmac("sha256", SIGNING_SECRET).update(base).digest("hex")}`;
}
