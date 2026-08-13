import { describe, expect, it } from "vitest";
import { formatSlackTextForReadability } from "../src/lambda/slack-events.js";

describe("formatSlackTextForReadability", () => {
  it("keeps conversational search replies readable without forcing bullets", () => {
    const text = [
      "はい、あります。近いのは「飲料メーカーA社 Webカタログチームのエラー調査」です。",
      "",
      "商用環境でアラートが発生した際にCloudWatch LogsやLambdaログをAIが読み込み、原因調査と対応方針の提案を支援する仕組みです。",
      "",
      "ほかには「製造業A社の開発リードタイム分析」もあります。"
    ].join("\n");

    expect(formatSlackTextForReadability(text)).toBe(
      [
        "はい、あります。近いのは「飲料メーカーA社 Webカタログチームのエラー調査」です。",
        "",
        "商用環境でアラートが発生した際にCloudWatch LogsやLambdaログをAIが読み込み、原因調査と対応方針の提案を支援する仕組みです。",
        "",
        "ほかには「製造業A社の開発リードタイム分析」もあります。"
      ].join("\n")
    );
  });

  it("keeps celebratory framing for scheduled daily shares", () => {
    const text = [
      "飲料業界の事例紹介！山田さんたちの飲料メーカーA社 Webカタログチームのエラー調査事例です:tada:",
      "",
      "AIが原因調査と対応方針の提案を支援する取り組みです。"
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
