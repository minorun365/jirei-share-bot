import { describe, expect, it, vi } from "vitest";
import { fullJitterDelayMs, isTransientErrorName, withBackoff } from "../src/domain/retry.js";

const options = { maxAttempts: 4, baseDelayMs: 30_000, maxDelayMs: 120_000 };

describe("Exponential Backoff with Full Jitter", () => {
  it("doubles the ceiling per failure and caps it", () => {
    const top = () => 0.999999;
    expect(fullJitterDelayMs(1, options, top)).toBe(29_999);
    expect(fullJitterDelayMs(2, options, top)).toBe(59_999);
    expect(fullJitterDelayMs(3, options, top)).toBe(119_999);
    expect(fullJitterDelayMs(6, options, top)).toBe(119_999);
    expect(fullJitterDelayMs(3, options, () => 0)).toBe(0);
  });

  it("retries until the attempt succeeds and waits a jittered delay between", async () => {
    const sleep = vi.fn(async (_ms: number) => {});
    const attempt = vi
      .fn()
      .mockResolvedValueOnce({ kind: "retry", reason: "ServiceUnavailableException" })
      .mockResolvedValueOnce({ kind: "retry", reason: "ServiceUnavailableException" })
      .mockResolvedValueOnce({ kind: "done", value: "ok" });

    const result = await withBackoff(attempt, { ...options, sleep, random: () => 0.5 });

    expect(result).toEqual({ value: "ok", attempts: 3 });
    expect(sleep.mock.calls.map(([ms]) => ms)).toEqual([15_000, 30_000]);
  });

  it("gives up after maxAttempts and reports the last reason", async () => {
    const sleep = vi.fn(async (_ms: number) => {});
    const attempt = vi.fn(async () => ({ kind: "retry" as const, reason: "ThrottlingException" }));

    const result = await withBackoff(attempt, { ...options, sleep, random: () => 0 });

    expect(attempt).toHaveBeenCalledTimes(4);
    expect(sleep).toHaveBeenCalledTimes(3);
    expect(result).toMatchObject({ attempts: 4, exhaustedReason: "ThrottlingException" });
  });

  it("treats only congestion-like errors as transient", () => {
    expect(isTransientErrorName("ServiceUnavailableException")).toBe(true);
    expect(isTransientErrorName("ThrottlingException")).toBe(true);
    expect(isTransientErrorName("ValidationException")).toBe(false);
    expect(isTransientErrorName("AccessDeniedException")).toBe(false);
    expect(isTransientErrorName(undefined)).toBe(false);
  });
});
