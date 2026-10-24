import { describe, expect, it } from "vitest";
import { buildSlackQueueMessage, parseSlackQueueMessage } from "../src/lambda/slack-events.js";

describe("Slack event queue messages", () => {
  it("orders messages per Slack thread and deduplicates by event ID", () => {
    const message = buildSlackQueueMessage({
      type: "event_callback",
      event_id: "Ev123",
      event: {
        type: "app_mention",
        channel: "C123",
        ts: "1784109763.897939",
        thread_ts: "1784109700.000001"
      }
    });

    expect(message.messageGroupId).toBe("C123:1784109700.000001");
    expect(message.messageDeduplicationId).toBe("Ev123");
    expect(parseSlackQueueMessage(message.body).payload.event_id).toBe("Ev123");
  });

  it("uses the message timestamp for a new root thread", () => {
    const message = buildSlackQueueMessage({
      type: "event_callback",
      event_id: "Ev456",
      event: {
        type: "app_mention",
        channel: "C456",
        ts: "1784109763.000002"
      }
    });

    expect(message.messageGroupId).toBe("C456:1784109763.000002");
  });

  it("hashes identifiers that exceed the SQS 128 character limit", () => {
    const message = buildSlackQueueMessage({
      type: "event_callback",
      event_id: `Ev${"x".repeat(200)}`,
      event: {
        type: "app_mention",
        channel: `C${"y".repeat(200)}`,
        ts: "1784109763.000003"
      }
    });

    expect(message.messageGroupId).toMatch(/^[a-f0-9]{64}$/);
    expect(message.messageDeduplicationId).toMatch(/^[a-f0-9]{64}$/);
  });

  it("rejects malformed queue messages", () => {
    expect(() => parseSlackQueueMessage(JSON.stringify({ mode: "unknown" }))).toThrow(
      "Invalid Slack event queue message"
    );
  });
});
