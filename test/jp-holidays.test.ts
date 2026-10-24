import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { isJapaneseHoliday, japaneseHolidaysOf, noPostReason, toJstDate } from "../src/domain/jp-holidays.js";

// 内閣府の公式CSVを正解として、公表済みの2026〜2027年の祝日を1日も過不足なく再現できることを確かめる
const officialHolidays = readFileSync(new URL("./fixtures/cao-holidays-2026-2027.csv", import.meta.url), "utf8")
  .split("\n")
  .filter((line) => /^\d{4}\//.test(line))
  .map((line) => {
    const [year, month, day] = line.split(",")[0]!.split("/").map(Number);
    return `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  });

describe("japaneseHolidaysOf", () => {
  it.each([2026, 2027])("matches the Cabinet Office list for %i", (year) => {
    const expected = officialHolidays.filter((value) => value.startsWith(`${year}-`)).sort();
    expect(expected.length).toBeGreaterThan(0);
    expect([...japaneseHolidaysOf(year)].sort()).toEqual(expected);
  });

  it("treats the Silver Week weekday between two holidays as a holiday", () => {
    expect(isJapaneseHoliday({ year: 2026, month: 9, day: 21 })).toBe(true);
    expect(isJapaneseHoliday({ year: 2026, month: 9, day: 22 })).toBe(true);
    expect(isJapaneseHoliday({ year: 2026, month: 9, day: 23 })).toBe(true);
    expect(isJapaneseHoliday({ year: 2026, month: 9, day: 24 })).toBe(false);
  });

  it("keeps working for years the Cabinet Office has not published yet", () => {
    for (let year = 2028; year <= 2099; year += 1) {
      expect(japaneseHolidaysOf(year).size).toBeGreaterThanOrEqual(16);
    }
  });
});

describe("noPostReason", () => {
  it("skips the year-end and New Year holidays from Dec 29 to Jan 3", () => {
    for (const day of [29, 30, 31]) {
      expect(noPostReason({ year: 2026, month: 12, day })).toBe("year_end_holidays");
    }
    expect(noPostReason({ year: 2027, month: 1, day: 1 })).toBe("japanese_holiday");
    for (const day of [2, 3]) {
      expect(noPostReason({ year: 2027, month: 1, day })).toBe("year_end_holidays");
    }
  });

  it("posts on the ordinary weekdays around the year-end break", () => {
    expect(noPostReason({ year: 2026, month: 12, day: 28 })).toBeUndefined();
    expect(noPostReason({ year: 2027, month: 1, day: 4 })).toBeUndefined();
  });
});

describe("toJstDate", () => {
  it("uses the Japan calendar date even when UTC is still the previous day", () => {
    expect(toJstDate(new Date("2026-09-21T15:30:00Z"))).toEqual({ year: 2026, month: 9, day: 22 });
    expect(toJstDate(new Date("2026-09-24T03:00:00Z"))).toEqual({ year: 2026, month: 9, day: 24 });
  });
});
