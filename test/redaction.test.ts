import { describe, expect, it } from "vitest";
import { sanitizeSlackText } from "../src/domain/redaction.js";

describe("sanitizeSlackText", () => {
  it("keeps normal internal case summaries postable", () => {
    const result = sanitizeSlackText(
      "製造業A社向けの案件で、開発部の佐藤さんが提案書ドラフト支援を作りました。"
    );

    expect(result.status).toBe("safe");
    expect(result.flags).toEqual([]);
  });

  it("masks emails and phone numbers", () => {
    const result = sanitizeSlackText("連絡先は taro@example.com と 090-1234-5678 です。");

    expect(result.status).toBe("masked");
    expect(result.text).toContain("[メールアドレス省略]");
    expect(result.text).toContain("[電話番号省略]");
    expect(result.flags).toEqual(["email", "phone"]);
  });

  it("keeps internal identifiers that contain date-like numbers", () => {
    const result = sanitizeSlackText("E2E_TEST_20260702_022354 の社内検証です。");

    expect(result.status).toBe("safe");
    expect(result.text).toContain("E2E_TEST_20260702_022354");
  });

  it("blocks obvious tokens", () => {
    const result = sanitizeSlackText("api_key=AKIA1234567890ABCDEF");

    expect(result.status).toBe("blocked");
    expect(result.flags).toContain("secret_or_token");
  });

  it("blocks tokens consistently across repeated checks", () => {
    expect(sanitizeSlackText("token=xoxb-test-token").status).toBe("blocked");
    expect(sanitizeSlackText("token=xoxb-test-token").status).toBe("blocked");
  });

  it("blocks Tavily API keys", () => {
    const result = sanitizeSlackText("Tavily key is tvly-test-abcdefghijklmnopqrstuvwxyz1234567890");

    expect(result.status).toBe("blocked");
    expect(result.flags).toContain("secret_or_token");
  });
});
