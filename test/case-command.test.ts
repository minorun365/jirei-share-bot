import { describe, expect, it } from "vitest";
import { buildCaseTitle, parseCaseCommand } from "../src/domain/case-command.js";

describe("parseCaseCommand", () => {
  it("parses explicit registration requests", () => {
    const command = parseCaseCommand("<@U0BE2H4MY7R> 事例登録: E2E_TEST_20260702 営業支援AIの検証をしました。");

    expect(command).toEqual({
      kind: "register",
      content: "E2E_TEST_20260702 営業支援AIの検証をしました。",
      testMarker: "E2E_TEST_20260702"
    });
  });

  it("parses search requests", () => {
    const command = parseCaseCommand("<@U0BE2H4MY7R> E2E_TEST_20260702 の事例を教えて");

    expect(command).toEqual({
      kind: "search",
      query: "E2E_TEST_20260702 の",
      testMarker: "E2E_TEST_20260702"
    });
  });

  it("limits automatic delete to marked test data", () => {
    const command = parseCaseCommand("<@U0BE2H4MY7R> テストデータ削除 E2E_TEST_20260702");

    expect(command).toEqual({
      kind: "delete_test",
      marker: "E2E_TEST_20260702"
    });
  });
});

describe("buildCaseTitle", () => {
  it("uses E2E marker as a stable title", () => {
    expect(buildCaseTitle("E2E_TEST_20260702 営業支援AIの検証をしました。")).toBe("E2E_TEST_20260702 の事例");
  });
});
