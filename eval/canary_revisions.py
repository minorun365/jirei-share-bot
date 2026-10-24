"""版履歴つき更新のcanary。デプロイ済みの事例テーブル・版履歴テーブルへ、架空の E2E_TEST_ 事例で
登録 → 追記 → 元に戻す を通し、最後に必ず消す。Slack・S3・画像生成には触れない。

実行例（リポジトリルート）。テーブル名は CloudFormation の CaseShareBotStateStack のリソースから確かめる:
    AWS_REGION=ap-northeast-1 BEDROCK_MODEL_ID=<モデルIDまたは推論プロファイルID> \\
    CASES_TABLE_NAME=<事例テーブル名> CASE_REVISIONS_TABLE_NAME=<版履歴テーブル名> \\
    uv run --with-requirements agentcore-runtime/requirements.txt python eval/canary_revisions.py

どこかで期待と違えば非0で終了する。残存データがあっても非0。
Runtimeロールの権限はこのスクリプトでは確かめられないので、IAMシミュレーターと合わせて使う。
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
missing = [key for key in ("CASES_TABLE_NAME", "CASE_REVISIONS_TABLE_NAME") if not os.environ.get(key)]
if missing:
    sys.exit(f"環境変数 {', '.join(missing)} を設定してください（デプロイ済みのテーブル名）。")
for key, value in {
    "THREAD_STATE_TABLE_NAME": "unused",
    "SHARE_LOG_TABLE_NAME": "unused",
    "CASE_ASSETS_BUCKET_NAME": "unused",
    "GRAPHIC_GENERATION_ENABLED": "false",
}.items():
    os.environ.setdefault(key, value)
sys.path.insert(0, str(ROOT / "agentcore-runtime"))

import main  # noqa: E402
from boto3.dynamodb.conditions import Key  # noqa: E402

main.put_case_markdown = lambda record: None
main.save_thread_state = lambda record: None
main.schedule_case_graphic = lambda record: record
main.merge_case_summary = lambda current, incoming, raw, case_id="": f"{current} 追記: {raw}"[:400]

stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
case_id = f"case-E2E_TEST_{stamp}"
original_summary = f"E2E_TEST_{stamp} ザンプル商事の在庫最適化をAIで支援した架空の事例です。"
failures: list[str] = []


def check(condition: bool, label: str) -> None:
    print(("OK " if condition else "NG ") + label)
    if not condition:
        failures.append(label)


try:
    main.cases_table.put_item(Item={
        "case_id": case_id, "title": f"E2E_TEST_{stamp} ザンプル商事の在庫最適化", "summary": original_summary,
        "status": "ready", "project_or_customer": "ザンプル商事", "sensitive_flags": [], "keywords": [],
        "redaction_status": "safe", "test_marker": f"E2E_TEST_{stamp}",
        "approach": "需要予測モデルで発注量を自動提案し、店舗ごとの在庫を最適化した",
        "impact": "廃棄ロスを18%削減",
    })
    record = main.get_case_by_id(case_id)
    result = main.update_case_record(
        record,
        "ザンプル商事で欠品率が3割下がりました",
        extracted_fields={"impact": "欠品率が3割低下"},
    )
    check(result.get("ok") is True and result.get("action") == "update", f"追記できた（{result.get('action')}）")
    updated = main.get_case_by_id(case_id)
    check(int(updated.get("revision", 0)) == 1, "事例の版番号が1になった")
    impact = str(updated.get("impact", ""))
    check("20%" in impact and ("3割" in impact or "30%" in impact), f"効果に元の事実と新しい事実が両方残った（{impact[:60]}）")
    check("需要予測" in str(updated.get("approach", "")), "取り組みの元の内容が残った")
    revisions = main.case_revisions_table.query(KeyConditionExpression=Key("case_id").eq(case_id))["Items"]
    check(sorted(int(item["revision"]) for item in revisions) == [0, 1], "版0（更新前）と版1が履歴に残った")
    check(any(item["kind"] == "baseline" and item["record"]["summary"] == original_summary for item in revisions), "版0に更新前の紹介文が入っている")

    reverted = main.revert_case_to_revision(case_id)
    check(reverted.get("ok") is True, f"元に戻せた（{reverted.get('slack_text', '')[:40]}）")
    after = main.get_case_by_id(case_id)
    check(after.get("summary") == original_summary, "紹介文が更新前に戻った")
    check(int(after.get("revision", 0)) == 2, "戻す操作も版2として積まれた")

    stale = dict(record)  # 版番号が古いまま更新しようとすると止まること
    stale["revision"] = 1
    conflict = main.update_case_record(stale, "古い版からの更新")
    check(conflict.get("ok") is False and "別の依頼" in conflict.get("slack_text", ""), "古い版からの更新は衝突として止まった")
finally:
    main.cases_table.delete_item(Key={"case_id": case_id})
    for item in main.case_revisions_table.query(KeyConditionExpression=Key("case_id").eq(case_id))["Items"]:
        main.case_revisions_table.delete_item(Key={"case_id": case_id, "revision": item["revision"]})
    leftover = main.get_case_by_id(case_id) or main.case_revisions_table.query(KeyConditionExpression=Key("case_id").eq(case_id))["Items"]
    check(not leftover, "テストデータを消した")

print(f"\n{'FAILED' if failures else 'PASSED'} ({len(failures)} NG)")
sys.exit(1 if failures else 0)
