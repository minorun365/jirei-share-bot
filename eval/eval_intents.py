"""会話の意図判定の評価セットを本物のモデルで流し、どのツールを選んだかを採点する。

呼ぶのは Amazon Bedrock のモデルだけです。事例テーブル・版履歴・スレッド状態・Knowledge Base は
すべてメモリ上の偽物へ差し替え、eval/intent-cases.json の架空の事例（seed_cases）で動かします。
書き込みは記録するだけで、AWS 上のデータにも Slack にも触れません。

実行例（リポジトリルート）:
    AWS_REGION=ap-northeast-1 BEDROCK_MODEL_ID=<モデルIDまたは推論プロファイルID> \\
    uv run --with-requirements agentcore-runtime/requirements.txt \\
        python eval/eval_intents.py [--min-accuracy 0.95] [--only <id>]

合格率が --min-accuracy を下回ったら非0で終了します（プロンプトやモデルを変える前後の比較に使う）。
結果の詳細は eval/last-run.json に書き出します（.gitignore 済み）。
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "eval" / "intent-cases.json"

# main.py は読み込み時にテーブル名などを環境変数から読む。評価では偽物へ差し替えるので、名前はダミーでよい
for key, value in {
    "CASES_TABLE_NAME": "eval-cases",
    "CASE_REVISIONS_TABLE_NAME": "eval-case-revisions",
    "THREAD_STATE_TABLE_NAME": "eval-thread-state",
    "SHARE_LOG_TABLE_NAME": "eval-share-log",
    "CASE_ASSETS_BUCKET_NAME": "eval-case-assets",
    "CASE_KNOWLEDGE_BASE_ID": "EVAL",
    "GRAPHIC_GENERATION_ENABLED": "false",
    "TAVILY_SEARCH_ENABLED": "false",
    "EXTERNAL_URL_FETCH_ENABLED": "false",
    "GOOGLE_DRIVE_ENABLED": "false",
}.items():
    os.environ.setdefault(key, value)
sys.path.insert(0, str(ROOT / "agentcore-runtime"))

import main  # noqa: E402

BUSINESS_TOOLS = {
    "register_case_tool", "update_case_tool", "confirm_pending_update_tool", "ask_register_or_update_tool",
    "revert_case_tool", "search_cases_tool", "get_case_context_tool", "describe_case_tool",
    "case_next_action_tool", "get_case_graphic_tool", "describe_bot_tool", "chat_reply_tool",
    "unsupported_request_tool",
}

ACTION_TO_TOOL = {
    "register": "register_case_tool",
    "update": "update_case_tool",
    "revert": "revert_case_tool",
    "register_or_update_question": "ask_register_or_update_tool",
    "update_pending": "ask_register_or_update_tool",
    "duplicate_suspected": "ask_register_or_update_tool",
    "bot_info": "describe_bot_tool",
}


def load_eval_file(path: Path = CASES_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"seed_cases": data.get("seed_cases", []), "cases": data.get("cases", [])}


class MemoryCasesTable:
    """架空の事例を返す読み取り専用のテーブル。書き込みは記録だけする。"""

    def __init__(self, records: list[dict[str, Any]]):
        self._records = {record["case_id"]: dict(record) for record in records}
        self.writes: list[str] = []
        self.updated_case_ids: list[str] = []

    def get_item(self, **kwargs):
        record = self._records.get(kwargs["Key"]["case_id"])
        return {"Item": dict(record)} if record else {}

    def scan(self, **kwargs):
        return {"Items": [dict(record) for record in self._records.values()]}

    def put_item(self, **kwargs):
        self.writes.append("put_item")
        return {}

    def update_item(self, **kwargs):
        self.writes.append("update_item")
        self.updated_case_ids.append(kwargs["Key"]["case_id"])
        values = kwargs.get("ExpressionAttributeValues", {})
        return {"Attributes": {"case_id": kwargs["Key"]["case_id"], "title": values.get(":title", ""), "summary": values.get(":summary", "")}}

    def delete_item(self, **kwargs):
        self.writes.append("delete_item")
        return {}


class MemoryRevisionsTable:
    def put_item(self, **kwargs):
        return {}

    def get_item(self, **kwargs):
        return {}


def _bigrams(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", str(text or "")).lower()
    return {normalized[index:index + 2] for index in range(len(normalized) - 1)}


def make_fake_retrieval(records: list[dict[str, Any]]):
    """Knowledge Base の代わりに、文字の2-gramの重なりで架空の事例を並べる。"""

    def fake_retrieve(query: str, number_of_results: int, record_call: bool = False) -> dict[str, Any]:
        if record_call:
            main.record_tool_call("retrieve_cases_tool")
        query_grams = _bigrams(query)
        scored = []
        for record in records:
            text = " ".join(str(record.get(key) or "") for key in ("title", "summary", "project_or_customer", "approach", "impact"))
            overlap = len(query_grams & _bigrams(text)) / max(len(query_grams), 1)
            scored.append((0.72 + 0.2 * min(1.0, overlap / 0.3), record, text))
        scored.sort(key=lambda item: item[0], reverse=True)
        results = [
            {"case_ids": [record["case_id"]], "score": round(score, 3), "excerpt": main.truncate(text, 900)}
            for score, record, text in scored[: max(1, min(int(number_of_results or 5), 8))]
        ]
        retrieved = [result["case_ids"][0] for result in results]
        main._retrieved_case_ids.set(main.merge_case_ids(main._retrieved_case_ids.get(), retrieved))
        main._retrieval_results.set(results)
        return {"ok": True, "query": query, "results": results}

    return fake_retrieve


def install_fakes(seed_cases: list[dict[str, Any]], state: dict[str, Any] | None) -> MemoryCasesTable:
    table = MemoryCasesTable(seed_cases)
    main.cases_table = table
    main.case_revisions_table = MemoryRevisionsTable()
    main.retrieve_cases_from_knowledge_base = make_fake_retrieval(seed_cases)
    main.put_case_markdown = lambda record: None
    main.save_thread_state = lambda record: None
    main.save_pending_update = lambda *args, **kwargs: None
    main.schedule_case_graphic = lambda record: record
    main.get_current_thread_state = lambda payload: state
    return table


def prepare_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """聞き返し中の状態に時刻が無ければ「いま預けた」ことにする（古い預かりを試すケースは時刻を書く）。"""
    if not state:
        return None
    prepared = dict(state)
    if prepared.get("pending_action") and not prepared.get("updated_at"):
        prepared["updated_at"] = datetime.now(timezone.utc).isoformat()
    return prepared


async def run_case(case: dict[str, Any], seed_cases: list[dict[str, Any]]) -> dict[str, Any]:
    table = install_fakes(seed_cases, prepare_state(case.get("state")))
    payload = {
        "mode": "slack_app_mention",
        "text": case["text"],
        "channel_id": "C_EVAL",
        "message_ts": "1790000000.000100",
        "thread_ts": "1790000000.000001",
        "user_id": "U_EVAL",
        "post_to_channel": False,
        "thread_messages": [
            *(
                {"text": message["text"], "user": "U_BOT" if message.get("bot") else "U_OTHER", **({"bot_id": "B_BOT"} if message.get("bot") else {})}
                for message in case.get("thread", [])
            ),
            {"text": case["text"], "user": "U_EVAL", "is_current": True},
        ],
    }
    try:
        result = await main.invoke(payload)
    except Exception as exc:  # 評価では例外も1件の失敗として数える
        result = {"ok": False, "action": "exception", "error": type(exc).__name__}
    tools = [name for name in (result.get("agent") or {}).get("tool_calls", []) if name in BUSINESS_TOOLS]
    # 採点は「実際に起きたこと」で行う。書き込み系ツールが呼ばれても関門が止めて聞き返したなら、聞き返しとして数える
    effective = set(tools)
    if not table.writes:
        effective -= {"register_case_tool", "update_case_tool", "revert_case_tool", "confirm_pending_update_tool"}
    effective |= {ACTION_TO_TOOL[result.get("action")]} if result.get("action") in ACTION_TO_TOOL else set()
    ok = bool(effective & set(case["expect"])) and not (effective & set(case.get("forbid", [])))
    # 追記先まで決まっているケースは、書いた事例が正しいかも採点する
    if case.get("expect_case_id"):
        ok = ok and table.updated_case_ids == [case["expect_case_id"]]
    # 書かずに追記先を提案して預け直すケースは、提案した事例を採点する
    if case.get("expect_proposed_case_id"):
        ok = ok and not table.writes and result.get("proposed_case_id") == case["expect_proposed_case_id"]
    return {"id": case["id"], "pass": ok, "tools": tools, "effective": sorted(effective), "action": result.get("action"), "writes": table.writes, "updated_case_ids": table.updated_case_ids}


CLASSIFICATION_FAILURES: list[str] = []
_real_classify = main.classify_intent


def counting_classify(payload: dict[str, Any]) -> dict[str, Any]:
    result = _real_classify(payload)
    if result.get("intent") == "unknown":
        CLASSIFICATION_FAILURES.append(str(payload.get("text", ""))[:30])
    return result


async def main_async(args: argparse.Namespace) -> int:
    main.classify_intent = counting_classify
    data = load_eval_file()
    cases = data["cases"]
    if args.only:
        cases = [case for case in cases if case["id"] == args.only]
    results = []
    for case in cases:
        main._agent = None  # 1件ごとに会話履歴を持ち越さない
        result = await run_case(case, data["seed_cases"])
        results.append(result)
        mark = "OK " if result["pass"] else "NG "
        print(f"{mark} {result['id']:<42} tools={result['tools']} action={result['action']} updated={result['updated_case_ids']}", flush=True)
    passed = sum(1 for result in results if result["pass"])
    accuracy = passed / len(results) if results else 0.0
    print(f"\n{passed}/{len(results)} passed ({accuracy:.0%})")
    out = ROOT / "eval" / "last-run.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if CLASSIFICATION_FAILURES:
        # 意図判定が失敗すると「判定不能」として書き込みが止まり、点数に紛れて気づけない。1件でもあれば不合格
        print(f"意図判定の失敗 {len(CLASSIFICATION_FAILURES)}件: {CLASSIFICATION_FAILURES}")
        return 1
    return 0 if accuracy >= args.min_accuracy else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-accuracy", type=float, default=0.95)
    parser.add_argument("--only", default="")
    sys.exit(asyncio.run(main_async(parser.parse_args())))
