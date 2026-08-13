import { describe, expect, it } from "vitest";
import { isProcessableSlackEvent } from "../src/lambda/slack-events.js";

describe("isProcessableSlackEvent", () => {
  it("accepts app_mention in a public channel", () => {
    expect(isProcessableSlackEvent({ type: "app_mention", channel: "C0123456789" })).toBe(true);
  });

  it("ignores app_mention inside a DM (DM support is not adopted)", () => {
    expect(isProcessableSlackEvent({ type: "app_mention", channel: "D0123456789" })).toBe(false);
  });

  it("ignores plain channel messages without mention", () => {
    expect(isProcessableSlackEvent({ type: "message", channel: "C0123456789" })).toBe(false);
  });

  it("ignores DM messages (message.im is not subscribed)", () => {
    expect(isProcessableSlackEvent({ type: "message", channel: "D0123456789" })).toBe(false);
  });
});
