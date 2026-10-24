import { describe, expect, it } from "vitest";
import { formatSlackTextForReadability } from "../src/lambda/slack-events.js";

describe("formatSlackTextForReadability", () => {
  it("keeps conversational search replies readable without forcing bullets", () => {
    const text = [
      "はい、あります。近いのは「あおば運輸 配車システムのエラー調査」です。",
      "",
      "配車システムでアラートが出たときに、CloudWatch LogsやLambdaのログをAIが読み、原因の切り分けと対応案づくりを手伝う仕組みです。",
      "",
      "ほかには「みどり書房の発注業務の所要時間分析」もあります。"
    ].join("\n");

    expect(formatSlackTextForReadability(text)).toBe(
      [
        "はい、あります。近いのは「あおば運輸 配車システムのエラー調査」です。",
        "",
        "配車システムでアラートが出たときに、CloudWatch LogsやLambdaのログをAIが読み、原因の切り分けと対応案づくりを手伝う仕組みです。",
        "",
        "ほかには「みどり書房の発注業務の所要時間分析」もあります。"
      ].join("\n")
    );
  });

  it("keeps celebratory framing for scheduled daily shares", () => {
    const text = [
      "運送業界の事例紹介！中村さんたちのあおば運輸 配車システムのエラー調査事例です:tada:",
      "",
      "AIが原因の切り分けと対応案づくりを手伝う取り組みです。"
    ].join("\n");

    expect(formatSlackTextForReadability(text)).toBe(text);
  });

  it("collapses excessive blank lines", () => {
    expect(formatSlackTextForReadability("本文\n\n\n・概要: テスト\n\n\n\n詳しくはメンションしてください。")).toBe(
      ["本文", "", "・概要: テスト", "", "詳しくはメンションしてください。"].join("\n")
    );
  });

  it("restores escaped line breaks returned by an AgentCore tool argument", () => {
    const escaped = "確認しました。\\n\\n・Google Slides資料を読み取り成功\\n・Qiita記事を読み取り成功\\n\\nすべて取得できています。";

    expect(formatSlackTextForReadability(escaped)).toBe(
      [
        "確認しました。",
        "",
        "・Google Slides資料を読み取り成功",
        "・Qiita記事を読み取り成功",
        "",
        "すべて取得できています。"
      ].join("\n")
    );
  });
});
