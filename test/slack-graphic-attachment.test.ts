import { describe, expect, it } from "vitest";
import { graphicsForThreadResponse } from "../src/lambda/slack-events.js";

// 2026-08-04、スレッドで「グラレコできた？」と聞かれた Bot が推測で答え、
// 投稿されることのないチャンネルを利用者に見に行かせた。Runtime 側が
// action: "graphic" で画像を返すようにしたので、その添付が落ちないことを固定する。
describe("thread graphic attachment", () => {
  const graphicResponse = {
    ok: true,
    action: "graphic",
    case_id: "case-20260804060335-c3893e1f",
    slack_text: "「製造業A社 AIエージェント基盤開発支援」のグラレコはこちらです！",
    graphic_s3_key: "cases/case-20260804060335-c3893e1f/graphic.png",
    graphic_mime_type: "image/png",
    graphic_alt_text: "製造業A社 AIエージェント基盤開発支援 のグラフィックレコーディング画像"
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
        slack_text: "「製造業A社 AIエージェント基盤開発支援」のグラレコは生成待ちで、あと3分ほどで出来上がります。"
      })
    ).toHaveLength(0);
    expect(graphicsForThreadResponse({ ok: true, action: "register", graphic_s3_key: "cases/foo/graphic.png" })).toHaveLength(0);
  });
});
