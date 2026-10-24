import { createHmac } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  buildSharedCaseThreadStateItem,
  extractFileShareTs,
  verifySlackSignature
} from "../src/lambda/slack-events.js";

describe("verifySlackSignature", () => {
  it("accepts valid Slack signatures", () => {
    const body = JSON.stringify({ type: "event_callback" });
    const timestamp = "1000";
    const secret = "test-secret";
    const signature = sign(secret, timestamp, body);

    expect(
      verifySlackSignature(
        body,
        {
          "x-slack-request-timestamp": timestamp,
          "x-slack-signature": signature
        },
        secret,
        1000
      )
    ).toBe(true);
  });

  it("rejects stale timestamps", () => {
    const body = JSON.stringify({ type: "event_callback" });
    const timestamp = "1000";
    const secret = "test-secret";
    const signature = sign(secret, timestamp, body);

    expect(
      verifySlackSignature(
        body,
        {
          "x-slack-request-timestamp": timestamp,
          "x-slack-signature": signature
        },
        secret,
        2000
      )
    ).toBe(false);
  });
});

describe("daily share thread state", () => {
  it("builds a thread state item that points replies at the shared case", () => {
    expect(
      buildSharedCaseThreadStateItem({
        caseId: "case-123",
        channelId: "C123",
        messageTs: "1783330000.123456",
        sharedAt: "2026-07-06T09:00:00.000Z",
        expiresAt: 1791104400
      })
    ).toEqual({
      thread_key: "C123:1783330000.123456",
      case_id: "case-123",
      channel_id: "C123",
      thread_ts: "1783330000.123456",
      updated_at: "2026-07-06T09:00:00.000Z",
      expires_at: 1791104400
    });
  });
});

describe("extractFileShareTs", () => {
  it("reads the Slack message ts from a completed file upload response", () => {
    expect(
      extractFileShareTs(
        {
          files: [
            {
              shares: {
                public: {
                  C123: [{ ts: "1783330000.123456" }]
                }
              }
            }
          ]
        },
        "C123"
      )
    ).toBe("1783330000.123456");
  });
});

function sign(secret: string, timestamp: string, body: string): string {
  const base = `v0:${timestamp}:${body}`;
  return `v0=${createHmac("sha256", secret).update(base).digest("hex")}`;
}
