// 日本の国民の祝日（振替休日・国民の休日を含む）を外部APIなしで判定する。
// 規則は祝日法の現行版に合わせ、2026年以降を対象にする。
// 春分・秋分は国立天文台の暦要項と一致する近似式（1980〜2099年で有効）で求める。
// 即位礼など法律で個別に定まる臨時の祝日は規則から導けないため、EXTRA_HOLIDAYS へ手で足す。

const EXTRA_HOLIDAYS: readonly string[] = [];

const MIN_SUPPORTED_YEAR = 2026;
const MAX_SUPPORTED_YEAR = 2099;

// 年末年始の休み（祝日ではないが、同僚が休んでいる日）。期間を変えるときはここだけ直す
const YEAR_END_FIRST_DAY = 29; // 12月
const NEW_YEAR_LAST_DAY = 3; // 1月

export type NoPostReason = "japanese_holiday" | "year_end_holidays";

export interface JstDate {
  readonly year: number;
  readonly month: number;
  readonly day: number;
}

const JST_OFFSET_MS = 9 * 60 * 60 * 1000;

// 日本には夏時間が無いので、UTC+9 の固定オフセットで暦日を取る
export function toJstDate(now: Date): JstDate {
  const shifted = new Date(now.getTime() + JST_OFFSET_MS);
  return { year: shifted.getUTCFullYear(), month: shifted.getUTCMonth() + 1, day: shifted.getUTCDate() };
}

export function formatJstDate(date: JstDate): string {
  return `${date.year}-${String(date.month).padStart(2, "0")}-${String(date.day).padStart(2, "0")}`;
}

// 同僚が休んでいる日には日次共有を投稿しない。休む理由を返し、平日なら undefined
export function noPostReason(date: JstDate): NoPostReason | undefined {
  if (isJapaneseHoliday(date)) return "japanese_holiday";
  if ((date.month === 12 && date.day >= YEAR_END_FIRST_DAY) || (date.month === 1 && date.day <= NEW_YEAR_LAST_DAY)) {
    return "year_end_holidays";
  }
  return undefined;
}

export function isJapaneseHoliday(date: JstDate): boolean {
  if (date.year < MIN_SUPPORTED_YEAR || date.year > MAX_SUPPORTED_YEAR) {
    throw new RangeError(`祝日判定の対象外の年です: ${date.year}`);
  }
  return japaneseHolidaysOf(date.year).has(formatJstDate(date));
}

export function japaneseHolidaysOf(year: number): ReadonlySet<string> {
  const base = new Set<string>(
    [
      ymd(year, 1, 1), // 元日
      nthMonday(year, 1, 2), // 成人の日
      ymd(year, 2, 11), // 建国記念の日
      ymd(year, 2, 23), // 天皇誕生日
      ymd(year, 3, vernalEquinoxDay(year)), // 春分の日
      ymd(year, 4, 29), // 昭和の日
      ymd(year, 5, 3), // 憲法記念日
      ymd(year, 5, 4), // みどりの日
      ymd(year, 5, 5), // こどもの日
      nthMonday(year, 7, 3), // 海の日
      ymd(year, 8, 11), // 山の日
      nthMonday(year, 9, 3), // 敬老の日
      ymd(year, 9, autumnalEquinoxDay(year)), // 秋分の日
      nthMonday(year, 10, 2), // スポーツの日
      ymd(year, 11, 3), // 文化の日
      ymd(year, 11, 23), // 勤労感謝の日
      ...EXTRA_HOLIDAYS.filter((value) => value.startsWith(`${year}-`))
    ]
  );

  const holidays = new Set(base);

  // 振替休日: 祝日が日曜なら、その後の最初の「祝日でない日」を休日にする
  for (const value of base) {
    const date = parse(value);
    if (date.getUTCDay() !== 0) continue;
    const next = new Date(date);
    do {
      next.setUTCDate(next.getUTCDate() + 1);
    } while (holidays.has(iso(next)));
    holidays.add(iso(next));
  }

  // 国民の休日: 前日と翌日が祝日に挟まれた平日（例: 敬老の日と秋分の日の間）
  for (const value of base) {
    const between = parse(value);
    between.setUTCDate(between.getUTCDate() + 1);
    const after = new Date(between);
    after.setUTCDate(after.getUTCDate() + 1);
    if (!holidays.has(iso(between)) && base.has(iso(after)) && between.getUTCDay() !== 0) {
      holidays.add(iso(between));
    }
  }

  return holidays;
}

function vernalEquinoxDay(year: number): number {
  return Math.floor(20.8431 + 0.242194 * (year - 1980) - Math.floor((year - 1980) / 4));
}

function autumnalEquinoxDay(year: number): number {
  return Math.floor(23.2488 + 0.242194 * (year - 1980) - Math.floor((year - 1980) / 4));
}

function nthMonday(year: number, month: number, nth: number): string {
  const firstWeekday = new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
  const firstMonday = 1 + ((8 - firstWeekday) % 7);
  return ymd(year, month, firstMonday + (nth - 1) * 7);
}

function ymd(year: number, month: number, day: number): string {
  return formatJstDate({ year, month, day });
}

function parse(value: string): Date {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(Date.UTC(year!, month! - 1, day!));
}

function iso(date: Date): string {
  return date.toISOString().slice(0, 10);
}
