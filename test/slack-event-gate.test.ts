import { describe, expect, it } from "vitest";
import { isProcessableSlackEvent, shouldAnswerThreadReply } from "../src/lambda/slack-events.js";

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

describe("thread replies without mention", () => {
  const bot = { userId: "UBOT000001", botId: "BBOT000001" };
  const reply = (text: string) => ({
    type: "message",
    channel: "C0123456789",
    user: "UUSER00001",
    text,
    thread_ts: "1000.000100",
    ts: "1000.000400"
  });
  // 利用者の質問 → Bot の回答 → Bot の画像 → 利用者がメンションなしで追質問、という実際の流れ
  const thread = [
    { ts: "1000.000100", user: "UUSER00001", text: "<@UBOT000001> 架空事例について教えて" },
    { ts: "1000.000200", user: "UBOT000001", bot_id: "BBOT000001", text: "「架空事例」が近そうです。" },
    { ts: "1000.000300", user: "UBOT000001", bot_id: "BBOT000001", files: [{ name: "jirei-share-graphic.png" }] },
    { ts: "1000.000400", user: "UUSER00001", text: "ほかの製品との違いは？" }
  ];

  it("passes the ACK gate only for public-channel thread replies by humans", () => {
    expect(isProcessableSlackEvent(reply("違いは？"))).toBe(true);
    const { thread_ts: _omit, ...topLevel } = reply("親投稿");
    expect(isProcessableSlackEvent(topLevel)).toBe(false);
    expect(isProcessableSlackEvent({ ...reply("親投稿"), ts: "1000.000100" })).toBe(false);
    expect(isProcessableSlackEvent({ ...reply("編集"), subtype: "message_changed" })).toBe(false);
    expect(isProcessableSlackEvent({ ...reply("Bot投稿"), bot_id: "BOTHER0001" })).toBe(false);
    expect(isProcessableSlackEvent({ ...reply("DM"), channel: "D0123456789" })).toBe(false);
    expect(isProcessableSlackEvent({ ...reply("private"), channel: "G0123456789" })).toBe(false);
  });

  it("answers a follow-up that comes right after the bot's answer", () => {
    expect(shouldAnswerThreadReply(reply("ほかの製品との違いは？"), thread, bot)).toBe(true);
  });

  it("does not answer twice when the reply also mentions the bot (app_mention handles it)", () => {
    expect(shouldAnswerThreadReply(reply("<@UBOT000001> 違いは？"), thread, bot)).toBe(false);
  });

  it("does not cut into a conversation addressed to another person", () => {
    expect(shouldAnswerThreadReply(reply("<@UOTHER0001> これどう思う？"), thread, bot)).toBe(false);
  });

  it("stays quiet when the previous message is from a human", () => {
    const humanTalk = [
      ...thread.slice(0, 3),
      { ts: "1000.000350", user: "UOTHER0001", text: "面白いですね" }
    ];
    expect(shouldAnswerThreadReply(reply("ですよね"), humanTalk, bot)).toBe(false);
  });
});
