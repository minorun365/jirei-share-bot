// 一時的な失敗（モデルの混雑など）を、間隔を倍々に広げながら乱数でずらして呼び直す
// （Exponential Backoff with Full Jitter）。SDK の再試行は数秒で尽きるので、その外側で分単位に待つ。

export interface BackoffOptions {
  /** 1回目を含めた最大の試行回数 */
  readonly maxAttempts: number;
  /** 1回目の失敗のあとに待つ上限（ミリ秒）。以降は倍々に広げる */
  readonly baseDelayMs: number;
  /** 1回あたりの待ちの上限（ミリ秒） */
  readonly maxDelayMs: number;
  readonly sleep?: (ms: number) => Promise<void>;
  readonly random?: () => number;
}

export type AttemptOutcome<T> =
  | { readonly kind: "done"; readonly value: T }
  | { readonly kind: "retry"; readonly reason: string; readonly value?: T };

export interface BackoffResult<T> {
  readonly value: T | undefined;
  readonly attempts: number;
  /** 最後まで retry のまま終わったら、その理由 */
  readonly exhaustedReason?: string;
}

// n 回目の失敗のあとの待ち：0 〜 min(上限, 基準 × 2^(n-1)) の一様乱数
export function fullJitterDelayMs(failedAttempt: number, options: BackoffOptions, random: () => number = Math.random): number {
  const ceiling = Math.min(options.maxDelayMs, options.baseDelayMs * 2 ** Math.max(0, failedAttempt - 1));
  return Math.floor(random() * ceiling);
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export async function withBackoff<T>(
  attempt: (attemptNumber: number) => Promise<AttemptOutcome<T>>,
  options: BackoffOptions,
  onRetry?: (info: { attempt: number; reason: string; delayMs: number }) => void
): Promise<BackoffResult<T>> {
  const sleep = options.sleep ?? defaultSleep;
  const random = options.random ?? Math.random;
  let last: AttemptOutcome<T> | undefined;
  for (let n = 1; n <= options.maxAttempts; n += 1) {
    last = await attempt(n);
    if (last.kind === "done") {
      return { value: last.value, attempts: n };
    }
    if (n < options.maxAttempts) {
      const delayMs = fullJitterDelayMs(n, options, random);
      onRetry?.({ attempt: n, reason: last.reason, delayMs });
      await sleep(delayMs);
    }
  }
  return {
    value: last?.kind === "retry" ? last.value : undefined,
    attempts: options.maxAttempts,
    ...(last?.kind === "retry" ? { exhaustedReason: last.reason } : {})
  };
}

// 呼び直せば通る見込みのある失敗の種別（Bedrock・AgentCore の混雑と一時的な内部エラー）
const TRANSIENT_ERROR_NAMES = new Set([
  "ServiceUnavailableException",
  "ThrottlingException",
  "ModelThrottledException",
  "ModelNotReadyException",
  "InternalServerException",
  "InternalFailure",
  "TooManyRequestsException",
  "RequestTimeout",
  "TimeoutError"
]);

export function isTransientErrorName(name: string | undefined): boolean {
  return name !== undefined && TRANSIENT_ERROR_NAMES.has(name);
}
