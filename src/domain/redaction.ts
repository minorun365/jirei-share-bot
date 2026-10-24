export type RedactionStatus = "safe" | "masked" | "needs_review" | "blocked";

export interface RedactionResult {
  readonly status: RedactionStatus;
  readonly text: string;
  readonly flags: readonly string[];
}

const EMAIL_PATTERN = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi;
const PHONE_PATTERN = /(?<![A-Za-z0-9_])(?:\+81[-\s]?)?0\d{1,4}[-\s]?\d{1,4}[-\s]?\d{3,4}(?![A-Za-z0-9_])/g;
const TOKEN_PATTERN = /\b(?:xox[baprs]-[A-Za-z0-9-]+|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16})\b/;
const TAVILY_API_KEY_PATTERN = /\btvly-[A-Za-z0-9_-]{20,}\b/i;
const PASSWORD_HINT_PATTERN = /(password|passwd|pwd|パスワード|api[_ -]?key|secret|token)\s*[:=]\s*\S+/i;
const CUSTOMER_CONTACT_PATTERN = /(?:顧客担当者|お客様担当|先方担当|ご担当者)[^\n。]*(?:さん|様|氏)/g;

export function sanitizeSlackText(input: string): RedactionResult {
  let text = input;
  const flags: string[] = [];

  text = replaceWithFlag(text, EMAIL_PATTERN, "[メールアドレス省略]", flags, "email");
  text = replaceWithFlag(text, PHONE_PATTERN, "[電話番号省略]", flags, "phone");
  text = replaceWithFlag(text, CUSTOMER_CONTACT_PATTERN, "[顧客担当者名省略]", flags, "customer_contact");

  if (TOKEN_PATTERN.test(text) || TAVILY_API_KEY_PATTERN.test(text) || PASSWORD_HINT_PATTERN.test(text)) {
    return {
      status: "blocked",
      text: "[投稿禁止情報を検出しました]",
      flags: [...new Set([...flags, "secret_or_token"])]
    };
  }

  const uniqueFlags = [...new Set(flags)];
  return {
    status: uniqueFlags.length > 0 ? "masked" : "safe",
    text,
    flags: uniqueFlags
  };
}

function replaceWithFlag(
  input: string,
  pattern: RegExp,
  replacement: string,
  flags: string[],
  flag: string
): string {
  return input.replace(pattern, () => {
    flags.push(flag);
    return replacement;
  });
}
