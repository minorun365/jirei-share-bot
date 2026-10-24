export type CaseCommand =
  | {
      readonly kind: "register";
      readonly content: string;
      readonly testMarker?: string;
    }
  | {
      readonly kind: "search";
      readonly query: string;
      readonly testMarker?: string;
    }
  | {
      readonly kind: "delete_test";
      readonly marker: string;
    }
  | {
      readonly kind: "update";
      readonly content: string;
    }
  | {
      readonly kind: "help";
    };

const TEST_MARKER_PATTERN = /\bE2E_TEST_[A-Za-z0-9_-]+\b/;
const REGISTER_PATTERN = /(事例登録|登録して|登録しといて|登録|追加して|追加しといて|覚えて|保存して|保存)/;
const SEARCH_PATTERN = /(検索|探して|探す|教えて|ありますか|ある[？?]?|事例ある|事例は|事例を)/;
const UPDATE_PATTERN = /(追記|更新|補足|追加情報|ブラッシュアップ)/;
const DELETE_PATTERN = /(テストデータ削除|テスト削除|削除)/;

export function parseCaseCommand(text: string): CaseCommand {
  const normalized = normalizeSlackText(text);
  const testMarker = extractTestMarker(normalized);

  if (!normalized) {
    return { kind: "help" };
  }

  if (DELETE_PATTERN.test(normalized) && testMarker) {
    return { kind: "delete_test", marker: testMarker };
  }

  if (REGISTER_PATTERN.test(normalized)) {
    return withOptionalTestMarker(
      {
        kind: "register",
        content: cleanupLeadingCommandText(normalized, REGISTER_PATTERN)
      },
      testMarker
    );
  }

  if (UPDATE_PATTERN.test(normalized)) {
    return {
      kind: "update",
      content: cleanupLeadingCommandText(normalized, UPDATE_PATTERN)
    };
  }

  if (SEARCH_PATTERN.test(normalized) || testMarker) {
    return withOptionalTestMarker(
      {
        kind: "search",
        query: cleanupSearchText(normalized)
      },
      testMarker
    );
  }

  if (normalized.length >= 24) {
    return withOptionalTestMarker(
      {
        kind: "register",
        content: normalized
      },
      testMarker
    );
  }

  return { kind: "help" };
}

export function normalizeSlackText(text: string): string {
  return text
    .replace(/<@[^>]+>/g, " ")
    .replace(/&lt;@[^&]+&gt;/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export function extractTestMarker(text: string): string | undefined {
  return TEST_MARKER_PATTERN.exec(text)?.[0];
}

export function buildCaseTitle(content: string): string {
  const explicitTitle = /(?:タイトル|件名)\s*[:：]\s*([^。\n]+)/.exec(content)?.[1]?.trim();
  if (explicitTitle) {
    return truncate(explicitTitle, 64);
  }

  const marker = extractTestMarker(content);
  if (marker) {
    return `${marker} の事例`;
  }

  const firstSentence = content.split(/[。\n]/).find((part) => part.trim().length > 0)?.trim() ?? content;
  return truncate(firstSentence.replace(/^(事例|取り組み|案件)\s*[:：]?\s*/, ""), 64);
}

export function buildSearchQuery(command: Extract<CaseCommand, { kind: "search" }>): string {
  return (command.testMarker ?? command.query).trim();
}

function cleanupLeadingCommandText(text: string, pattern: RegExp): string {
  const cleaned = text
    .replace(new RegExp(`^${pattern.source}\\s*[:：、。-]?\\s*`), "")
    .replace(/^[\s:：、。-]+/, "")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned.length > 0 ? cleaned : text;
}

function cleanupSearchText(text: string): string {
  const globalPattern = new RegExp(SEARCH_PATTERN.source, "g");
  const cleaned = text
    .replace(globalPattern, " ")
    .replace(/^[\s:：、。-]+/, "")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned.length > 0 ? cleaned : text;
}

function truncate(text: string, maxLength: number): string {
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength - 1)}…`;
}

function withOptionalTestMarker<T extends { readonly kind: "register" | "search" }>(
  command: T,
  testMarker: string | undefined
): T | (T & { readonly testMarker: string }) {
  if (!testMarker) {
    return command;
  }
  return { ...command, testMarker };
}
