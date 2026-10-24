import { describe, expect, it } from "vitest";
import { buildGraphicFilename, graphicsForThreadResponse, isGraphicInThread } from "../src/lambda/slack-events.js";

// 2026-08-04、スレッドで「グラレコできた？」と聞かれた Bot が推測で答え、
// 投稿されることのないチャンネルを利用者に見に行かせた。Runtime 側が
// action: "graphic" で画像を返すようにしたので、その添付が落ちないことを固定する。
describe("thread graphic attachment", () => {
  const graphicResponse = {
    ok: true,
    action: "graphic",
    case_id: "case-20260101000000-e0a10102",
    slack_text: "「みどり書房 発注予測AI導入支援」のグラレコはこちらです！",
    graphic_s3_key: "cases/case-20260101000000-e0a10102/graphic.png",
    graphic_mime_type: "image/png",
    graphic_alt_text: "みどり書房 発注予測AI導入支援 のグラフィックレコーディング画像"
  };

  it("attaches the graphic when the agent answers a graphic request", () => {
    const graphics = graphicsForThreadResponse(graphicResponse);

    expect(graphics).toHaveLength(1);
    expect(graphics[0]?.s3_key).toBe(graphicResponse.graphic_s3_key);
    expect(graphics[0]?.mime_type).toBe("image/png");
  });

  it("keeps attaching graphics for search and describe replies", () => {
    expect(graphicsForThreadResponse({ ...graphicResponse, action: "describe" })).toHaveLength(1);
    expect(
      graphicsForThreadResponse({
        ok: true,
        action: "search",
        graphics: [{ s3_key: "cases/foo/graphic.png" }]
      })
    ).toHaveLength(1);
  });

  it("does not attach a graphic when the agent has none to show", () => {
    expect(
      graphicsForThreadResponse({
        ok: true,
        action: "graphic",
        slack_text: "「みどり書房 発注予測AI導入支援」のグラレコは生成待ちで、あと3分ほどで出来上がります。"
      })
    ).toHaveLength(0);
    expect(graphicsForThreadResponse({ ok: true, action: "register", graphic_s3_key: "cases/foo/graphic.png" })).toHaveLength(0);
  });
});

describe("graphic already posted in the thread", () => {
  const graphic = {
    s3_key: "cases/case-e2e-fictional/graphic.png",
    title: "架空商事様 在庫最適化 のグラフィックレコーディング画像",
    alt_text: "架空商事様 在庫最適化 のグラフィックレコーディング画像"
  };

  it("recognizes the search-time upload (title without the graphic suffix) as the same picture", () => {
    // 検索の画像タイトルは事例名だけ、詳細回答では「〜 のグラフィックレコーディング画像」になる
    const thread = [{ ts: "1.1", files: [{ name: "jirei-share-graphic.png", title: "架空商事様 在庫最適化" }] }];
    expect(isGraphicInThread(graphic, thread)).toBe(true);
  });

  it("recognizes new uploads by the fingerprint in the file name", () => {
    const thread = [{ ts: "1.1", files: [{ name: buildGraphicFilename(graphic, "image/png"), title: "改名後の事例名" }] }];
    expect(isGraphicInThread(graphic, thread)).toBe(true);
  });

  it("still attaches a different case's picture", () => {
    const thread = [{ ts: "1.1", files: [{ name: "jirei-share-graphic.png", title: "別の架空事例" }] }];
    expect(isGraphicInThread(graphic, thread)).toBe(false);
    expect(isGraphicInThread({ s3_key: "cases/x/graphic.png" }, thread)).toBe(false);
  });

  it("keeps the file name free of the internal case id", () => {
    const name = buildGraphicFilename(graphic, "image/png");
    expect(name).toMatch(/^jirei-share-graphic-[0-9a-f]{10}\.png$/);
    expect(name).not.toContain("case-e2e");
  });
});
