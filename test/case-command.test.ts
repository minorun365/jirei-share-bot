import { describe, expect, it } from "vitest";
import { buildCaseTitle, parseCaseCommand } from "../src/domain/case-command.js";

describe("parseCaseCommand", () => {
  it("parses explicit registration requests", () => {
    const command = parseCaseCommand("<@U0BOTEXAMPLE> 事例登録: E2E_TEST_20260702 店舗の発注AIを検証しました。");

    expect(command).toEqual({
      kind: "register",
      content: "E2E_TEST_20260702 店舗の発注AIを検証しました。",
      testMarker: "E2E_TEST_20260702"
    });
  });

  it("parses search requests", () => {
    const command = parseCaseCommand("<@U0BOTEXAMPLE> E2E_TEST_20260702 の事例を教えて");

    expect(command).toEqual({
      kind: "search",
      query: "E2E_TEST_20260702 の",
      testMarker: "E2E_TEST_20260702"
    });
  });

  it("limits automatic delete to marked test data", () => {
    const command = parseCaseCommand("<@U0BOTEXAMPLE> テストデータ削除 E2E_TEST_20260702");

    expect(command).toEqual({
      kind: "delete_test",
      marker: "E2E_TEST_20260702"
    });
  });
});

describe("buildCaseTitle", () => {
  it("uses E2E marker as a stable title", () => {
    expect(buildCaseTitle("E2E_TEST_20260702 店舗の発注AIを検証しました。")).toBe("E2E_TEST_20260702 の事例");
  });
});
