"""main.py の軽量スモークテスト。

環境変数をスタブして main.py を import できることと、
update_case_record の「追記重複ガード」「メタ文 summary の書き直し」「返信整形の失敗分離」を
AWS 未接続のまま検証する（DynamoDB / Bedrock はフェイクに差し替え）。

実行例（リポジトリルートから）:
    uv run --with pytest --with boto3 --with bedrock-agentcore \
        --with strands-agents --with strands-agents-tools --with google-genai \
        pytest agentcore-runtime/tests/test_main_smoke.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

# main.py はモジュールレベルで必須環境変数と boto3 クライアントを初期化するため、
# import より前にダミー値を注入する（API 呼び出しは発生しない）
os.environ.setdefault("CASES_TABLE_NAME", "dummy-cases")
os.environ.setdefault("THREAD_STATE_TABLE_NAME", "dummy-thread-state")
os.environ.setdefault("SHARE_LOG_TABLE_NAME", "dummy-share-log")
os.environ.setdefault("CASE_ASSETS_BUCKET_NAME", "dummy-bucket")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "dummy")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "dummy")
os.environ.setdefault("GRAPHIC_GENERATION_ENABLED", "false")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402


@pytest.fixture(autouse=True)
def _no_intent_model_call(monkeypatch):
    # 単体テストではモデルを呼ばない。意図判定の質は eval/eval_intents.py で本物のモデルを使って測る
    monkeypatch.setattr(main, "classify_intent", lambda payload: {})


class FakeRevisionsTable:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.fail = False

    def put_item(self, **kwargs):
        if self.fail:
            raise RuntimeError("AccessDenied")
        self.items.append(kwargs["Item"])
        return {}

    def get_item(self, **kwargs):
        key = kwargs["Key"]
        for item in reversed(self.items):
            if item["case_id"] == key["case_id"] and item["revision"] == key["revision"]:
                return {"Item": item}
        return {}


class FakeCasesTable:
    def __init__(self) -> None:
        self.last_update_kwargs: dict | None = None

    def update_item(self, **kwargs):
        self.last_update_kwargs = kwargs
        values = kwargs["ExpressionAttributeValues"]
        return {
            "Attributes": {
                "case_id": kwargs["Key"]["case_id"],
                "title": values[":title"],
                "summary": values[":summary"],
                "status": values[":status"],
                "redaction_status": values[":redaction_status"],
                "sensitive_flags": values[":flags"],
                "keywords": values[":keywords"],
                "updated_at": values[":updated_at"],
            }
        }


def base_record() -> dict:
    return {
        "case_id": "case-20260709000000-abcdef01",
        "title": "テスト事例",
        "summary": "既存の概要です。生成AIで問い合わせ対応を効率化しました",
        "status": "ready",
        "redaction_status": "safe",
        "sensitive_flags": [],
        "keywords": [],
    }


def response_record(**overrides) -> dict:
    """質問応答と日次共有の文体テストに使う、表示項目を持った事例。"""
    record = {
        "case_id": "case-20260709000000-fedcba98",
        "title": "生成AI活用入門講座",
        "summary": "店舗スタッフ向けに、生成AIの基礎と生成AIチャットを使った業務効率化を紹介する講座です。",
        "status": "ready",
        "customer_industry": "社内AI活用",
        "contact_hint": "中村（業務改善チーム）",
        "department": "業務改善部",
        "updated_at": "2026-07-09T00:00:00+00:00",
        "graphic_status": "ready",
        "graphic_s3_key": "cases/case-20260709000000-fedcba98/graphic.png",
        "graphic_mime_type": "image/png",
    }
    record.update(overrides)
    return record


def assert_complete_without_ellipsis(text: str, max_length: int | None = None) -> None:
    """短文化後の本文が人工的な省略記号で終わらず、文として完結していることを確認する。"""
    assert text
    assert "…" not in text
    assert text.endswith(("。", "！", "!", "？", "?"))
    if max_length is not None:
        assert len(text) <= max_length


class FakeBedrockRuntime:
    """converse の応答を固定するフェイク。text=None なら接続失敗を模擬する。"""

    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.call_count = 0

    def converse(self, **kwargs):
        self.call_count += 1
        if self.text is None:
            raise ConnectionError("bedrock unavailable in tests")
        return {"output": {"message": {"content": [{"text": self.text}]}}}


def stub_persistence(monkeypatch, bedrock_text: str | None = None) -> FakeCasesTable:
    fake = FakeCasesTable()
    monkeypatch.setattr(main, "cases_table", fake)
    monkeypatch.setattr(main, "bedrock_runtime", FakeBedrockRuntime(bedrock_text))
    monkeypatch.setattr(main, "put_case_markdown", lambda record: None)
    monkeypatch.setattr(main, "save_thread_state", lambda record: None)
    monkeypatch.setattr(main, "schedule_case_graphic", lambda record: record)
    revisions = FakeRevisionsTable()
    monkeypatch.setattr(main, "case_revisions_table", revisions)
    fake.revisions = revisions
    return fake


def test_import_smoke():
    assert callable(main.update_case_record)
    assert callable(main.format_case_paragraph)


def test_update_appends_summary(monkeypatch):
    fake = stub_persistence(monkeypatch)
    result = main.update_case_record(base_record(), "新しい成果として応答時間が半減しました")
    assert result["ok"] is True
    saved_summary = fake.last_update_kwargs["ExpressionAttributeValues"][":summary"]
    assert "追記: 新しい成果として応答時間が半減しました" in saved_summary


def test_update_skips_duplicate_append(monkeypatch):
    fake = stub_persistence(monkeypatch)
    record = base_record()
    record["summary"] = "既存の概要です。 追記: 新しい成果として応答時間が半減しました"
    result = main.update_case_record(record, "新しい成果として応答時間が半減しました")
    assert result["ok"] is True
    saved_summary = fake.last_update_kwargs["ExpressionAttributeValues"][":summary"]
    assert saved_summary == record["summary"]
    assert saved_summary.count("追記:") == 1


def test_update_meta_summary_rewritten_by_llm(monkeypatch):
    fake = stub_persistence(
        monkeypatch,
        bedrock_text="南北製パン向けにパンの焼き色検査をAIで自動化し、検査時間を短縮した事例。",
    )
    result = main.update_case_record(
        base_record(),
        "導入先は製パン会社B社ではなく南北製パン",
        extracted_fields={"summary": "導入先は製パン会社B社ではなく南北製パンが正しい情報です"},
    )
    assert result["ok"] is True
    saved_summary = fake.last_update_kwargs["ExpressionAttributeValues"][":summary"]
    assert "南北製パン" in saved_summary
    assert "が正しい" not in saved_summary
    assert "追記:" not in saved_summary


def test_update_duplicate_append_skips_llm_rewrite(monkeypatch):
    fake_bedrock = FakeBedrockRuntime("呼ばれてはいけない書き直し結果")
    fake = stub_persistence(monkeypatch)
    monkeypatch.setattr(main, "bedrock_runtime", fake_bedrock)
    record = base_record()
    record["summary"] = "既存の概要です。 追記: 新しい成果として応答時間が半減しました"
    result = main.update_case_record(record, "新しい成果として応答時間が半減しました")
    assert result["ok"] is True
    saved_summary = fake.last_update_kwargs["ExpressionAttributeValues"][":summary"]
    assert saved_summary == record["summary"]
    assert fake_bedrock.call_count == 0


def test_update_fallback_keeps_new_content_when_existing_summary_is_long(monkeypatch):
    fake = stub_persistence(monkeypatch)
    record = base_record()
    record["summary"] = "既存の取り組みを説明する完結文です。" + "安全な利用方法と運用ルールを整備しています。" * 16

    result = main.update_case_record(record, "受講後の満足度は95%でした")

    assert result["ok"] is True
    saved_summary = fake.last_update_kwargs["ExpressionAttributeValues"][":summary"]
    assert "受講後の満足度は95%" in saved_summary


def test_update_fallback_does_not_append_oversized_source_content(monkeypatch):
    fake = stub_persistence(monkeypatch)
    record = base_record()
    oversized_update = "資料から読み取った詳細情報" + "とても長い本文" * 80

    result = main.update_case_record(record, oversized_update)

    assert result["ok"] is True
    saved_summary = fake.last_update_kwargs["ExpressionAttributeValues"][":summary"]
    assert saved_summary == record["summary"]
    assert "追記:" not in saved_summary


def test_normalize_case_fields_does_not_trim_saved_summary_to_display_target():
    long_summary = "生成AIの基礎と安全な利用方法を説明します。" + "Claude Desktopの演習を行います。" * 6

    normalized = main.normalize_case_card_fields(
        "事例の原文",
        {"summary": long_summary},
        fill_defaults=False,
    )

    assert "Claude Desktopの演習" in normalized["summary"]
    assert len(normalized["summary"]) > main.CASE_BODY_MAX_CHARS


def test_build_summary_never_creates_a_mid_sentence_ellipsis():
    content = "生成AIの基礎と安全な利用方法を説明します。" + "Claude Desktopの演習を行います。" * 20

    summary = main.build_summary(content)

    assert "…" not in summary
    assert summary.endswith("。")
    assert len(summary) <= 360


def test_search_reply_answers_existence_question_without_daily_fanfare():
    record = response_record()

    reply = main.format_search_response(
        [record],
        "店舗スタッフ向けのAI講義資料はあります？",
        [],
    )

    assert reply.startswith(f"はい、あります。「{record['title']}」が近そうです。")
    assert "店舗スタッフ向けに" in reply
    assert "事例紹介！" not in reply
    assert ":tada:" not in reply
    assert not reply.rstrip().endswith("！")


def test_search_reply_mentions_other_candidates_calmly():
    primary = response_record()
    alternatives = [
        response_record(
            case_id="case-20260709000001-fedcba98",
            title="生成AI基礎講座",
            summary="生成AIの基本概念と安全な使い方を学ぶ講座です。",
        ),
        response_record(
            case_id="case-20260709000002-fedcba98",
            title="店舗スタッフ向けAI活用研修",
            summary="定型業務を題材にAI活用を体験する研修です。",
        ),
    ]

    reply = main.format_search_response(
        [primary, *alternatives],
        "店舗スタッフ向けのAI講義資料はあります？",
        [],
    )

    assert "ほかには" in reply
    assert all(f"「{record['title']}」" in reply for record in alternatives)
    assert "もあります。" in reply
    assert "事例紹介！" not in reply
    assert ":tada:" not in reply


def test_search_reply_acknowledges_follow_up_wording():
    record = response_record()

    for query in ["もっと汎用的なやつ", "ほかの事例", "別の案件", "汎用的なもの"]:
        reply = main.format_search_response([record], query, [])
        assert reply.startswith(f"それなら、「{record['title']}」が近そうです。"), query
        assert "事例紹介！" not in reply
        assert ":tada:" not in reply


def test_case_detail_reply_is_conversational_without_daily_fanfare():
    record = response_record()

    reply = main.format_case_detail(record)

    assert reply.startswith(f"「{record['title']}」ですね。")
    assert "店舗スタッフ向けに" in reply
    assert "事例紹介！" not in reply
    assert ":tada:" not in reply
    assert not reply.rstrip().endswith("！")
    assert "Slackで" in reply


def test_case_detail_skips_title_lead_when_bot_already_named_the_case_in_thread():
    record = response_record()
    title = record["title"]
    thread = [
        {"ts": "1.1", "text": "<@UBOT> 教えて"},
        {"ts": "1.2", "bot_id": "B1", "text": f"「{title}」が近そうです。"},
        {"ts": "1.3", "text": "<@UBOT> 基本機能は？", "is_current": True},
    ]

    reply = main.format_case_detail(record, "基本機能は？", "", thread)

    assert not reply.startswith(f"「{title}」ですね。")
    assert "店舗スタッフ向けに" in reply
    # Bot がまだ名指ししていないスレッド（利用者の発言だけ）では従来どおり前置きを付ける
    first = main.format_case_detail(record, "基本機能は？", "", thread[:1] + thread[2:])
    assert first.startswith(f"「{title}」ですね。")


def test_project_deep_dive_answers_known_impact_and_hands_off_unknown_product_detail():
    record = response_record(
        title="社内開発プロジェクト：AIコーディング支援で受け入れテストを自動化した開発",
        summary=(
            "社内開発チームがプルリクエストごとの自動テストと夜間の回帰試験で品質を担保し、"
            "人手の確認時間を45%削減した事例です。"
        ),
        project_or_customer="社内開発プロジェクト",
        impact="人手の確認時間を45%削減",
        contact_hint="中村、松本、鈴木、developer-a、developer-b",
        department="社内開発チーム",
    )

    reply = main.format_case_detail(record, "社内開発プロジェクトって何を作ってるの？どんなプロダクトなの？定量効果も知りたい")

    assert "プロダクトの中身までは登録情報にありませんでした" in reply
    assert "人手の確認時間を45%削減" in reply
    assert "45%削減したと記録されています" in reply
    assert "\n\n効果については" in reply
    assert "プロダクトの中身や定量効果の背景" in reply
    assert "Slackで" in reply
    assert "\n\nプロダクトの中身や定量効果の背景" in reply
    assert all(name in reply for name in ["中村さん", "松本さん", "鈴木さん", "developer-aさん", "developer-bさん"])
    assert record["summary"] not in reply


def test_case_detail_uses_grounded_agent_answer_and_appends_contact_handoff():
    record = response_record(contact_hint="中村", department="販売企画部")

    reply = main.format_case_detail(
        record,
        "現場ではどう進めたの？",
        "登録情報では、講義とハンズオンを組み合わせて進めています。",
    )

    assert "講義とハンズオンを組み合わせて" in reply
    assert record["summary"] not in reply
    assert "\n\n登録情報より踏み込んだ内容" in reply
    assert "Slackで中村さんか「販売企画部」に聞いてみてください" in reply


def test_search_with_deep_dive_question_also_bridges_to_registered_contact():
    record = response_record(
        impact="受講後アンケートで満足度90%でした",
        contact_hint="中村",
        department="販売企画部",
    )

    reply = main.format_search_response([record], "この講座の定量効果を詳しく教えて", [])

    assert "満足度90%" in reply
    assert "Slackで中村さんか「販売企画部」に聞いてみてください" in reply
    assert "気になる点があれば" in reply


def test_case_detail_without_contact_invites_channel_communication_without_inventing_person():
    record = response_record(
        contact_hint="",
        department="",
        owner_department="",
        owner_name="",
        team="",
    )

    reply = main.format_case_detail(record, "もっと詳しく知りたい")

    assert "聞き先がまだ登録されていません" in reply
    assert "このチャンネルで詳しい方を聞いてみてください" in reply
    assert all(name not in reply for name in ["中村", "松本", "鈴木", "高橋"])


def test_case_detail_link_request_detection_distinguishes_content_detail():
    assert main.is_case_detail_link_request("詳細リンクあります？") is True
    assert main.is_case_detail_link_request("URLを教えて") is True
    assert main.is_case_detail_link_request("詳細ページを教えて") is True
    assert main.is_case_detail_link_request("詳細URLは？") is True
    assert main.is_case_detail_link_request("詳細へのリンクは？") is True
    assert main.is_case_detail_link_request("効果の詳細を教えて") is False
    assert main.is_case_detail_link_request("このURLの内容を教えて") is False
    assert main.is_case_detail_link_request("このURLを読んで、事例の詳細を教えて") is False
    assert main.is_case_detail_link_request("このURLから事例の詳細を取得して") is False


def test_case_link_unavailable_reply_guides_user_to_registered_contact():
    record = response_record(external_source_urls=["https://example.com/internal-case-source"])

    reply = main.format_case_link_unavailable_reply(record)

    assert "詳細リンクを取得する機能はまだありません" in reply
    assert main.format_internal_people_with_honorifics(record["contact_hint"]) in reply
    assert record["department"] in reply
    assert "聞いて" in reply
    assert "https://" not in reply
    assert "事例紹介！" not in reply
    assert ":tada:" not in reply


def test_case_link_unavailable_reply_does_not_invent_a_contact():
    record = response_record(
        contact_hint="",
        department="",
        owner_department="",
        owner_name="",
        team="",
    )
    assert main.format_responsible_value(record) == ""

    reply = main.format_case_link_unavailable_reply(record)

    assert "詳細リンクを取得する機能はまだありません" in reply
    assert all(name not in reply for name in ["開発担当", "松本", "鈴木", "高橋"])
    assert "https://" not in reply


def test_daily_share_alone_keeps_fanfare_and_lively_ending():
    reply = main.format_daily_share(response_record())

    assert "事例紹介！" in reply
    assert ":tada:" in reply
    assert reply.rstrip().endswith(("！", "!"))


def test_daily_share_adds_san_to_every_internal_person_name():
    names = ["中村", "松本", "鈴木", "高橋", "伊藤", "渡辺", "小林", "加藤", "吉田"]
    record = response_record(
        title="あおば運輸 配車アプリ内製開発支援プロジェクト",
        customer_industry="運送",
        contact_hint="、".join(names),
        department="販売企画部",
        team="運行管理部 / デザイン部 / 営業部",
    )

    reply = main.format_daily_share(record)

    assert all(f"{name}さん" in reply for name in names)
    assert all(f"{name}、" not in reply for name in names[:-1])
    assert "販売企画部さん" not in reply
    assert "デザイン部さん" not in reply


def test_internal_person_honorific_does_not_duplicate_or_change_team_names():
    assert main.format_internal_people_with_honorifics("中村さん、松本") == "中村さん、松本さん"
    assert main.format_internal_people_with_honorifics("中村（販売企画部）") == "中村さん（販売企画部）"
    assert (
        main.format_internal_people_with_honorifics("中村 / developer-a / developer-b")
        == "中村さん / developer-aさん / developer-bさん"
    )
    assert main.format_internal_people_with_honorifics("開発担当（企画室）") == "開発担当（企画室）"
    assert main.format_internal_people_with_honorifics("デザイン部") == "デザイン部"


def test_display_tags_do_not_split_person_names_into_bare_search_keywords():
    tags = main.build_display_tags(
        response_record(contact_hint="中村さん、developer-aさん、developer-bさん")
    )

    assert all(name not in tags for name in ["中村", "developer-a", "developer-b"])


def test_shorten_slack_paragraph_completes_a_long_single_sentence():
    text = (
        "店舗スタッフ向けの講義で、生成AIの基本概念と安全な利用方法を説明し、"
        "Claude Desktopによる文章整理や要約や定型文作成を実演し、"
        "受講者が日常業務へ持ち帰れる演習まで扱う構成です。"
    )

    shortened = main.shorten_slack_paragraph(text, 80)

    assert_complete_without_ellipsis(shortened)
    assert shortened == text


def test_shorten_slack_paragraph_keeps_complete_sentences():
    text = (
        "生成AIを安全に使うための基礎を説明します。"
        "Claude Desktopで文章を整理する手順を実演します。"
        "最後に参加者自身の業務を題材に演習します。"
    )

    shortened = main.shorten_slack_paragraph(text, 55)

    assert_complete_without_ellipsis(shortened, 55)


def test_search_and_daily_replies_do_not_end_mid_sentence():
    long_summary = (
        "生成AIの基本概念と安全な利用方法を説明します。"
        "Claude Desktopによる文章整理や要約や定型文作成を実演します。"
        "受講者が自分の日常業務へ持ち帰れる演習も行います。"
    )
    record = response_record(summary=long_summary)

    search_reply = main.format_search_response([record], "店舗スタッフ向けの資料あります？", [])
    daily_reply = main.format_daily_share(record)

    assert "…" not in search_reply
    assert search_reply.rstrip().endswith(("。", "！", "!", "？", "?"))
    assert "…" not in daily_reply
    assert daily_reply.rstrip().endswith(("。", "！", "!", "？", "?"))


class FakeCaseLookupTable:
    def __init__(self, record: dict) -> None:
        self.record = record

    def get_item(self, **kwargs):
        if kwargs.get("Key", {}).get("case_id") == self.record["case_id"]:
            return {"Item": self.record}
        return {}


class FakeThreadStateLookupTable:
    def __init__(self, state: dict) -> None:
        self.state = state

    def get_item(self, **kwargs):
        return {"Item": self.state}


def test_get_case_context_tool_returns_only_grounding_fields(monkeypatch):
    record = response_record(
        problem="手作業が多い",
        approach="生成AIで自動化した",
        impact="作業時間を45%削減した",
        external_source_urls=["https://example.com/internal-source"],
    )
    monkeypatch.setattr(main, "cases_table", FakeCaseLookupTable(record))
    payload_token = main._current_payload.set({"mode": "slack_app_mention", "text": "詳しく教えて"})
    tool_calls_token = main._tool_calls.set([])
    try:
        result = json.loads(main.get_case_context_tool(case_id=record["case_id"]))
    finally:
        main._tool_calls.reset(tool_calls_token)
        main._current_payload.reset(payload_token)

    assert result["ok"] is True
    assert result["case"]["impact"] == "作業時間を45%削減した"
    assert "external_source_urls" not in result["case"]


def test_describe_tool_uses_question_specific_answer_and_contact_handoff(monkeypatch):
    record = response_record(contact_hint="中村", department="販売企画部")
    monkeypatch.setattr(main, "cases_table", FakeCaseLookupTable(record))
    monkeypatch.setattr(main, "save_thread_state", lambda _record: None)
    payload_token = main._current_payload.set(
        {"mode": "slack_app_mention", "text": "どんなプロダクトなの？", "channel_id": "C123", "thread_ts": "1.0"}
    )
    tool_calls_token = main._tool_calls.set([])
    tool_results_token = main._tool_results.set([])
    try:
        result = json.loads(
            main.describe_case_tool(
                case_id=record["case_id"],
                question="どんなプロダクトなの？",
                answer="登録情報では、プロダクトの具体的な中身までは分かりません。",
            )
        )
    finally:
        main._tool_results.reset(tool_results_token)
        main._tool_calls.reset(tool_calls_token)
        main._current_payload.reset(payload_token)

    assert "プロダクトの具体的な中身までは分かりません" in result["slack_text"]
    assert "Slackで中村さんか「販売企画部」に聞いてみてください" in result["slack_text"]


def test_chat_reply_cannot_overwrite_search_or_detail_result():
    prior = {
        "ok": True,
        "action": "search",
        "slack_text": "登録情報に基づく検索結果です。Slackで中村さんに聞いてみてください。",
    }
    tool_calls_token = main._tool_calls.set(["retrieve_cases_tool", "search_cases_tool", "chat_reply_tool"])
    tool_results_token = main._tool_results.set([prior])
    try:
        result = json.loads(main.chat_reply_tool(message="検索結果を自由に書き直します"))
        tracked_results = list(main._tool_results.get())
    finally:
        main._tool_results.reset(tool_results_token)
        main._tool_calls.reset(tool_calls_token)

    assert result == prior
    assert tracked_results == [prior]


def test_case_next_action_intent_covers_paraphrases_without_matching_unrelated_work():
    positives = [
        "このあと高橋さんはどうするのがいい？",
        "実装してみたけど、このあとどう進めるのがいい？",
        "次は何をすればよいですか？",
        "ここからどう進める？",
        "誰に聞けばいい？",
        "相談先を教えて",
        "次のアクションは？",
    ]
    negatives = [
        "明日の天気を教えて",
        "このコードを実装して",
        "ありがとう！",
        "別の事例もある？",
    ]

    assert all(main.is_case_next_action_request(text) for text in positives)
    assert all(not main.is_case_next_action_request(text) for text in negatives)


def test_bot_design_intent_covers_self_questions_without_stealing_case_questions():
    positives = [
        "君のグラレコ、文字化けしないけどプロンプトで工夫してるの？モデルは何？",
        "このBotのアーキテクチャを教えて",
        "事例共有くんはデータをどこに保存してる？",
        "あなた自身の検索方式はRAGなの？",
    ]
    negatives = [
        "この事例で使ったモデルは何？",
        "社内開発プロジェクトのアーキテクチャを教えて",
        "君が紹介した事例のモデルは何を使ってるの？",
        "あなたがさっき出した案件の技術構成を教えて",
        "画像生成AIの社内事例はある？",
        "このあとどう進めればいい？",
    ]

    assert all(main.is_bot_design_question(text) for text in positives)
    assert all(not main.is_bot_design_question(text) for text in negatives)


def test_bot_design_follow_up_uses_thread_context():
    thread_messages = [
        {"text": "このBotのグラレコはどう作っているの？"},
        {"bot_id": "B123", "text": "画像生成の仕組みを説明しました。"},
        {"text": "会話側のモデルは？", "is_current": True},
    ]

    assert main.is_bot_design_question("会話側のモデルは？", thread_messages)
    assert not main.is_bot_design_question(
        "会話側のモデルは？",
        [{"text": "この事例で使った技術を教えて"}],
    )
    assert not main.is_bot_design_question(
        "この事例のモデルは？",
        [{"text": "このBotのグラレコはどう作っているの？"}],
    )


def bot_design_reanswer_thread(current_text: str, include_failed_reanswer: bool = False) -> list[dict]:
    messages = [
        {
            "ts": "1.0",
            "text": "君のグラレコ文字化けが全然ないけど、プロンプトとかそういう工夫はしてるの？モデルは何使ってるの？",
        },
        {
            "ts": "1.1",
            "bot_id": "B123",
            "text": "グラレコ用のプロンプトやモデルは分かりません。",
        },
    ]
    if include_failed_reanswer:
        messages.extend(
            [
                {
                    "ts": "1.2",
                    "text": "改修したので、改めて松本さんの質問に答えてあげて！",
                },
                {
                    "ts": "1.3",
                    "bot_id": "B123",
                    "text": "なお、会話側はClaudeです。全体構成や検索方式も説明します。",
                },
                {
                    "ts": "1.4",
                    "text": "（なお…？",
                },
            ]
        )
    messages.append({"ts": "1.5", "text": current_text, "is_current": True})
    return messages


def test_bot_design_reanswer_resolves_original_question_and_addressee():
    current_text = "改修したので、改めて松本さんの質問に答えてあげて！"
    payload = {
        "text": current_text,
        "thread_messages": bot_design_reanswer_thread(current_text),
    }

    assert main.is_bot_design_question(current_text, payload["thread_messages"])
    assert main.resolve_bot_design_question(payload) == (
        "君のグラレコ文字化けが全然ないけど、プロンプトとかそういう工夫はしてるの？モデルは何使ってるの？"
    )
    assert main.resolve_bot_design_addressee(payload) == "松本さん"


def test_bot_design_second_reanswer_still_resolves_original_question_and_addressee():
    current_text = "ごめんもっかい、最初から丁寧に答えてあげて"
    payload = {
        "text": current_text,
        "thread_messages": bot_design_reanswer_thread(current_text, include_failed_reanswer=True),
    }

    assert main.is_bot_design_question(current_text, payload["thread_messages"])
    assert "グラレコ文字化け" in main.resolve_bot_design_question(payload)
    assert main.resolve_bot_design_addressee(payload) == "松本さん"


def enable_graphic_feature(monkeypatch):
    monkeypatch.setattr(main, "GRAPHIC_GENERATION_ENABLED", True)
    monkeypatch.setattr(main, "GEMINI_IMAGE_MODEL", "example-image-model")
    monkeypatch.setattr(main, "BEDROCK_MODEL_DISPLAY_NAME", "Example Bedrock model")


def test_graphic_design_reply_explains_when_optional_feature_is_disabled(monkeypatch):
    monkeypatch.setattr(main, "GRAPHIC_GENERATION_ENABLED", False)
    monkeypatch.setattr(main, "GEMINI_IMAGE_MODEL", "")

    reply = main.format_bot_design_reply("グラレコ画像はどう作っているの？")

    assert "任意機能" in reply
    assert "この環境では無効" in reply
    assert "``" not in reply


def test_graphic_design_reply_explains_model_prompt_and_limitations(monkeypatch):
    enable_graphic_feature(monkeypatch)
    reply = main.format_bot_design_reply(
        "君のグラレコ、文字化けしないけどプロンプトで工夫してるの？モデルは何？"
    )

    assert "example-image-model" in reply
    assert "2K・16:9" in reply
    assert "正確で読みやすい日本語" in reply
    assert "短い見出しとキーフレーズ" in reply
    assert "生成後にOCRで文字を修正しているわけではなく" in reply
    assert "完全にゼロとまでは保証できません" in reply
    assert "Example Bedrock model" in reply
    assert "グラレコ画像を作るモデルとは別" in reply
    assert not reply.startswith("なお")


def test_bot_architecture_reply_is_grounded_in_deployed_components():
    reply = main.format_bot_design_reply("このアプリの設計と検索方式、情報保護について教えて")

    assert "API Gateway" in reply
    assert "SQS" in reply
    assert "AgentCore Runtime" in reply
    assert "DynamoDBの登録データを正本" in reply
    assert "S3 Vectors" in reply
    assert "マスクまたはブロック" in reply





def test_case_next_action_uses_thread_context_and_prior_questions(monkeypatch):
    record = response_record(
        title="社内開発プロジェクト：AI駆動開発",
        contact_hint="中村",
        department="開発チーム",
    )
    state = {"thread_key": "C123:1.0", "case_id": record["case_id"]}
    monkeypatch.setattr(main, "cases_table", FakeCaseLookupTable(record))
    monkeypatch.setattr(main, "thread_state_table", FakeThreadStateLookupTable(state))
    monkeypatch.setattr(main, "save_thread_state", lambda _record: None)
    payload = {
        "mode": "slack_app_mention",
        "text": "改修してみたけど、このあと高橋さんはどうするのがいい？",
        "channel_id": "C123",
        "thread_ts": "1.0",
        "thread_messages": [
            {"text": "社内開発プロジェクトについて、何を作っていて定量効果はどれくらい？"},
            {"text": "どんなプロダクトなの？"},
            {"bot_id": "B123", "text": "登録済みの概要を回答しました。"},
            {"text": "改修してみたけど、このあと高橋さんはどうするのがいい？", "is_current": True},
        ],
    }
    payload_token = main._current_payload.set(payload)
    tool_calls_token = main._tool_calls.set([])
    tool_results_token = main._tool_results.set([])
    try:
        result = json.loads(main.case_next_action_tool(question=payload["text"]))
    finally:
        main._tool_results.reset(tool_results_token)
        main._tool_calls.reset(tool_calls_token)
        main._current_payload.reset(payload_token)

    assert result["action"] == "handoff"
    assert result["case_id"] == record["case_id"]
    assert result["slack_text"].startswith("高橋さんは、まず")
    assert "中村さんか「開発チーム」のメンバーにSlackで" in result["slack_text"]
    assert "プロダクトの中身や定量効果の背景" in result["slack_text"]
    assert "詳しい人との会話につなげ" in result["slack_text"]
    assert "このBotでできることから少し外れて" not in result["slack_text"]



def test_unsupported_tool_recovers_from_active_case_context(monkeypatch):
    record = response_record(contact_hint="中村", department="開発チーム")
    state = {"thread_key": "C123:1.0", "case_id": record["case_id"]}
    monkeypatch.setattr(main, "cases_table", FakeCaseLookupTable(record))
    monkeypatch.setattr(main, "thread_state_table", FakeThreadStateLookupTable(state))
    monkeypatch.setattr(main, "save_thread_state", lambda _record: None)
    payload = {
        "mode": "slack_app_mention",
        "text": "このあとどう進めればいい？",
        "channel_id": "C123",
        "thread_ts": "1.0",
    }
    payload_token = main._current_payload.set(payload)
    tool_calls_token = main._tool_calls.set([])
    tool_results_token = main._tool_results.set([])
    try:
        result = json.loads(main.unsupported_request_tool(question=payload["text"]))
    finally:
        main._tool_results.reset(tool_results_token)
        main._tool_calls.reset(tool_calls_token)
        main._current_payload.reset(payload_token)

    assert result["action"] == "handoff"
    assert "中村さん" in result["slack_text"]
    assert "使い方の例" not in result["slack_text"]


def test_explicitly_unrelated_request_stays_out_of_scope_even_in_case_thread(monkeypatch):
    record = response_record(contact_hint="中村", department="開発チーム")
    state = {"thread_key": "C123:1.0", "case_id": record["case_id"]}
    monkeypatch.setattr(main, "cases_table", FakeCaseLookupTable(record))
    monkeypatch.setattr(main, "thread_state_table", FakeThreadStateLookupTable(state))
    payload = {
        "mode": "slack_app_mention",
        "text": "明日の天気を教えて",
        "channel_id": "C123",
        "thread_ts": "1.0",
    }
    payload_token = main._current_payload.set(payload)
    tool_calls_token = main._tool_calls.set([])
    tool_results_token = main._tool_results.set([])
    try:
        result = json.loads(main.unsupported_request_tool(question=payload["text"]))
    finally:
        main._tool_results.reset(tool_results_token)
        main._tool_calls.reset(tool_calls_token)
        main._current_payload.reset(payload_token)

    assert result["action"] == "help"
    assert "このBotでできることから少し外れて" in result["slack_text"]


def test_describe_tool_link_reply_does_not_attach_graphic(monkeypatch):
    record = response_record()
    monkeypatch.setattr(main, "cases_table", FakeCaseLookupTable(record))
    payload_token = main._current_payload.set({"mode": "slack_app_mention", "text": "URLを教えて"})
    tool_calls_token = main._tool_calls.set([])
    tool_results_token = main._tool_results.set([])
    try:
        result = json.loads(main.describe_case_tool(case_id=record["case_id"], question="URLを教えて"))
    finally:
        main._tool_results.reset(tool_results_token)
        main._tool_calls.reset(tool_calls_token)
        main._current_payload.reset(payload_token)

    assert "詳細リンクを取得する機能はまだありません" in result["slack_text"]
    assert "graphic_s3_key" not in result
    assert "graphics" not in result


def test_invoke_returns_tool_result_from_copied_context_even_when_final_text_is_not_json(monkeypatch):
    expected = {
        "ok": True,
        "action": "register",
        "case_id": "case-20260715000000-deadbeef",
        "slack_text": "事例として登録しました。",
    }

    async def fake_run_agent(_prompt: str) -> str:
        await asyncio.to_thread(main.remember_tool_result, expected, "register_case_tool")
        return "事例として登録しました。"

    monkeypatch.setattr(main, "build_prompt", lambda payload, context=None: "test prompt")
    monkeypatch.setattr(main, "run_agent", fake_run_agent)

    result = asyncio.run(main.invoke({"mode": "slack_app_mention", "text": "この事例を登録して"}))

    assert result["ok"] is True
    assert result["action"] == "register"
    assert result["case_id"] == expected["case_id"]
    assert result["agent"]["tool_calls"] == ["register_case_tool"]


def test_invoke_does_not_mislabel_missing_business_tool_as_out_of_scope(monkeypatch):
    async def fake_run_agent(_prompt: str) -> str:
        return "業務ツールを呼ばない自然文"

    monkeypatch.setattr(main, "build_prompt", lambda payload, context=None: "test prompt")
    monkeypatch.setattr(main, "run_agent", fake_run_agent)

    result = asyncio.run(main.invoke({"mode": "slack_app_mention", "text": "この事例を登録して"}))

    assert result["ok"] is False
    assert result["action"] == "error"
    assert result["error"] == "business_tool_not_selected"
    assert "対象外" not in result["slack_text"]
    assert "登録・更新・検索は実行していません" in result["slack_text"]


def test_drive_canary_reports_fetch_metadata_without_content(monkeypatch):
    monkeypatch.setattr(
        main,
        "fetch_url_source",
        lambda url, max_chars: {
            "url": url,
            "ok": True,
            "method": "google_drive_api",
            "warnings": [],
            "attempts": [{"method": "google_drive_api", "ok": True, "readable": True, "chars": 1234}],
            "content_excerpt": "a" * max_chars,
        },
    )

    result = asyncio.run(
        main.invoke(
            {
                "mode": "drive_canary",
                "url": "https://drive.google.com/file/d/example-file-id/view",
            }
        )
    )

    assert result["ok"] is True
    assert result["action"] == "drive_canary"
    assert result["post_to_channel"] is False
    assert result["method"] == "google_drive_api"
    assert result["chars"] == 1000
    assert "content_excerpt" not in result


def test_update_reply_does_not_use_daily_share_fanfare(monkeypatch):
    stub_persistence(monkeypatch)

    result = main.update_case_record(base_record(), "新しい成果として応答時間が半減しました")

    assert "事例紹介！" not in result["slack_text"]
    assert ":tada:" not in result["slack_text"]


class FakeUrlResponse:
    """urlrequest.urlopen の戻り値を模擬する（with構文対応）。"""

    def __init__(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.data = data
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self, limit: int | None = None):
        return self.data if limit is None else self.data[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_url_points_to_pdf():
    assert main.url_points_to_pdf("https://example.com/docs/case.PDF") is True
    assert main.url_points_to_pdf("https://example.com/docs/case") is False


def test_pdf_document_fetch_uses_attachment_path(monkeypatch):
    monkeypatch.setattr(
        main.urlrequest,
        "urlopen",
        lambda request, timeout=None: FakeUrlResponse(b"%PDF-1.7 dummy", "application/pdf"),
    )
    captured: dict = {}

    def fake_extract(data: bytes, fmt: str, name: str):
        captured.update({"fmt": fmt, "name": name})
        return "converse_document", "画像PDFから読み取ったテキスト"

    monkeypatch.setattr(main, "extract_text_from_binary", fake_extract)
    result = main.try_pdf_document_fetch("https://example.com/docs/scan-case.pdf")
    assert result["ok"] is True
    assert result["text"] == "画像PDFから読み取ったテキスト"
    assert captured == {"fmt": "pdf", "name": "scan-case.pdf"}


def test_pdf_document_fetch_rejects_html_body(monkeypatch):
    monkeypatch.setattr(
        main.urlrequest,
        "urlopen",
        lambda request, timeout=None: FakeUrlResponse(b"<html>login required</html>", "text/html"),
    )
    result = main.try_pdf_document_fetch("https://example.com/docs/case.pdf")
    assert result["ok"] is False
    assert result["error"] == "not_pdf_content"


def test_drive_document_with_auth_terms_is_not_mistaken_for_login_wall(monkeypatch):
    """2026-07-14の回帰テスト: 認証設計を説明するDrive資料に
    「ログイン/パスワード/メールアドレス」があっても本文として読める。"""
    monkeypatch.setattr(main, "validate_fetch_url", lambda url: (True, "ok"))
    monkeypatch.setattr(main, "GOOGLE_DRIVE_ENABLED", True)
    document_text = (
        "利用者はメールアドレスとパスワードでログインし、認証後に機能を利用します。"
        "この方式をOpenSpecで定義し、開発チームの実装ルールとして整理しました。"
    ) * 20
    monkeypatch.setattr(
        main,
        "try_google_drive_fetch",
        lambda url: {"ok": True, "status_code": 200, "text": document_text},
    )

    result = main.fetch_url_source(
        "https://drive.google.com/file/d/example-file-id-1234567890/view",
        4000,
    )

    assert result["ok"] is True
    assert result["method"] == "google_drive_api"
    assert result["warnings"] == []
    assert result["attempts"][0]["readable"] is True


def test_external_url_fetch_is_disabled_until_explicitly_enabled(monkeypatch):
    monkeypatch.setattr(main, "EXTERNAL_URL_FETCH_ENABLED", False)
    monkeypatch.setattr(
        main,
        "fetch_url_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("URLを取得してはいけない")),
    )

    assert main.fetch_url_sources_for_prompt("https://example.com/case") == []


def test_slack_message_url_uses_configured_workspace(monkeypatch):
    monkeypatch.setattr(main, "SLACK_WORKSPACE_URL", "")
    assert main.build_slack_message_url("C0123456789", "123.456") == ""

    monkeypatch.setattr(main, "SLACK_WORKSPACE_URL", "https://example.slack.com")
    assert main.build_slack_message_url("C0123456789", "123.456") == (
        "https://example.slack.com/archives/C0123456789/p123456"
    )


def test_html_with_auth_terms_is_still_detected_as_login_wall(monkeypatch):
    """一般Webページでは従来どおり認証画面を検出する。"""
    monkeypatch.setattr(main, "validate_fetch_url", lambda url: (True, "ok"))
    login_page = "メールアドレスとパスワードを入力してログインしてください。" * 30
    monkeypatch.setattr(main, "try_tavily_extract", lambda url: {"ok": True, "text": login_page})
    monkeypatch.setattr(main, "try_http_request_fetch", lambda url: {"ok": False, "error": "empty_body"})
    monkeypatch.setattr(main, "URL_FETCH_BROWSER_ENABLED", False)

    result = main.fetch_url_source("https://example.com/login", 4000)

    assert result["ok"] is True
    assert "login_required_detected" in result["warnings"]
    assert result["attempts"][0]["readable"] is False


def test_drive_readable_file_does_not_call_permissions_api(monkeypatch):
    """明示的に渡されたDrive URLは、OAuthで読めれば共有範囲を再審査しない。"""
    monkeypatch.setattr(main, "get_google_drive_access_token", lambda: "token")

    def fake_drive_get(path: str, token: str, timeout: int = 30):
        if "/permissions?" in path:
            raise AssertionError("Driveの共有範囲をBot側で再審査してはいけない")
        if "/export?" in path:
            return "社内開発チームの取り組み資料です。".encode()
        return json.dumps(
            {
                "id": "file-id",
                "name": "社内開発事例",
                "mimeType": "application/vnd.google-apps.presentation",
            }
        ).encode()

    monkeypatch.setattr(main, "google_drive_api_get", fake_drive_get)

    result = main.try_google_drive_fetch(
        "https://docs.google.com/presentation/d/example-file-id-1234567890/view"
    )

    assert result == {
        "ok": True,
        "text": "社内開発チームの取り組み資料です。",
        "status_code": 200,
    }


def test_drive_file_inaccessible_is_reported_from_metadata_request(monkeypatch):
    """OAuthで読めない資料は共有範囲の推測をせず、アクセス不可として返す。"""
    from urllib import error as urlerror

    monkeypatch.setattr(main, "get_google_drive_access_token", lambda: "token")

    def fake_drive_get(path: str, token: str, timeout: int = 30):
        assert "/permissions?" not in path
        raise urlerror.HTTPError(path, 403, "Forbidden", None, None)

    monkeypatch.setattr(main, "google_drive_api_get", fake_drive_get)

    result = main.try_google_drive_fetch(
        "https://drive.google.com/file/d/example-file-id-1234567890/view"
    )

    assert result == {"ok": False, "error": "drive_file_not_accessible", "status_code": 403}


def test_drive_bot_side_error_reply_is_casual_and_split_into_paragraphs():
    main._fetched_url_sources.set(
        [{"url": "https://docs.google.com/presentation/d/example/edit", "ok": False, "error": "drive_credentials_unavailable"}]
    )

    reply = main.format_current_drive_failure_reply()

    assert "ごめん" in reply
    assert "送り直しは不要" in reply
    assert "Bot側" in reply
    assert "drive_credentials_unavailable" not in reply
    assert reply.count("\n\n") == 2


def test_drive_inaccessible_reply_is_casual_and_does_not_require_company_wide_share():
    main._fetched_url_sources.set(
        [{"url": "https://drive.google.com/file/d/example/view", "ok": False, "error": "drive_file_not_accessible"}]
    )

    reply = main.format_current_drive_failure_reply()

    assert "ごめん" in reply
    assert "送り直しは不要" in reply
    assert "直接アクセスできる状態" in reply
    assert "全社員" not in reply
    assert "社内全体共有" not in reply
    assert "drive_file_not_accessible" not in reply
    assert reply.count("\n\n") == 2


def test_fetch_url_source_prefers_pdf_branch(monkeypatch):
    monkeypatch.setattr(main, "validate_fetch_url", lambda url: (True, "ok"))
    monkeypatch.setattr(
        main,
        "try_pdf_document_fetch",
        lambda url: {"ok": True, "status_code": 200, "text": "スキャンPDFの本文" * 100},
    )

    def fail_tavily(url):
        raise AssertionError("PDF分岐で解決すべきなのにTavilyが呼ばれた")

    monkeypatch.setattr(main, "try_tavily_extract", fail_tavily)
    result = main.fetch_url_source("https://example.com/docs/case.pdf", 4000)
    assert result["ok"] is True
    assert result["method"] == "pdf_document"


def test_fetch_url_source_pdf_failure_falls_back(monkeypatch):
    monkeypatch.setattr(main, "validate_fetch_url", lambda url: (True, "ok"))
    monkeypatch.setattr(
        main, "try_pdf_document_fetch", lambda url: {"ok": False, "error": "pdf_download_failed"}
    )
    monkeypatch.setattr(main, "try_tavily_extract", lambda url: {"ok": True, "text": "HTML経由の本文" * 100})
    result = main.fetch_url_source("https://example.com/docs/case.pdf", 4000)
    assert result["ok"] is True
    assert result["method"] == "tavily_extract"
    assert result["attempts"][0]["method"] == "pdf_document"


def test_build_prompt_survives_decimal_thread_state(monkeypatch):
    """DynamoDB読み取りのDecimal（last_url_fetch_resultsのstatus_code等）で
    build_promptのjson.dumpsが落ちる2026-07-09障害の回帰テスト。"""
    from decimal import Decimal

    class FakeThreadStateTable:
        def get_item(self, **kwargs):
            return {
                "Item": {
                    "thread_key": "C123:1720000000.000100",
                    "case_id": "case-20260101000000-sample01",
                    "last_url_fetch_results": [
                        {
                            "url": "https://drive.google.com/file/d/x/view",
                            "ok": True,
                            "method": "google_drive_api",
                            "attempts": [{"method": "google_drive_api", "ok": True, "status_code": Decimal("200")}],
                        }
                    ],
                    "expires_at": Decimal("1720604400"),
                }
            }

    monkeypatch.setattr(main, "thread_state_table", FakeThreadStateTable())
    prompt = main.build_prompt(
        {
            "mode": "slack_app_mention",
            "text": "登録できた？",
            "channel_id": "C123",
            "thread_ts": "1720000000.000100",
        }
    )
    assert '"status_code": 200' in prompt


def test_decimal_to_native_converts_nested():
    from decimal import Decimal

    converted = main.decimal_to_native(
        {"a": Decimal("200"), "b": [Decimal("1.5")], "c": {"d": Decimal("3")}, "e": "text"}
    )
    assert converted == {"a": 200, "b": [1.5], "c": {"d": 3}, "e": "text"}
    assert isinstance(converted["a"], int)
    assert isinstance(converted["b"][0], float)


class FakeGraphicScanTable:
    """DynamoDB scanはFilterExpression適用前にLimit件を読むため、ハッシュキー順
    (due_epochとは無関係)でItemsを返す。その順序を模擬するフェイク。"""

    def __init__(self, items: list[dict]) -> None:
        self.items = items

    def scan(self, **kwargs):
        return {"Items": self.items}


def test_scan_due_graphic_cases_picks_oldest_due_first(monkeypatch):
    """2026-07-09の回帰テスト: scanが登録順と無関係な順序でItemsを返しても、
    due_epochが最も古い候補が選ばれることを確認する（他候補にstarvationしない）。"""
    items = [
        {"case_id": "case-new", "status": "ready", "graphic_status": "pending", "graphic_due_epoch": 300},
        {"case_id": "case-old", "status": "ready", "graphic_status": "pending", "graphic_due_epoch": 100},
        {"case_id": "case-mid", "status": "ready", "graphic_status": "pending", "graphic_due_epoch": 200},
    ]
    monkeypatch.setattr(main, "cases_table", FakeGraphicScanTable(items))
    result = main.scan_due_graphic_cases(1)
    assert [record["case_id"] for record in result] == ["case-old"]


def test_scan_due_graphic_cases_respects_limit_after_sorting(monkeypatch):
    items = [
        {"case_id": "case-b", "status": "ready", "graphic_status": "pending", "graphic_due_epoch": 200},
        {"case_id": "case-a", "status": "ready", "graphic_status": "pending", "graphic_due_epoch": 100},
        {"case_id": "case-c", "status": "ready", "graphic_status": "pending", "graphic_due_epoch": 300},
    ]
    monkeypatch.setattr(main, "cases_table", FakeGraphicScanTable(items))
    result = main.scan_due_graphic_cases(2)
    assert [record["case_id"] for record in result] == ["case-a", "case-b"]


def test_update_reply_format_failure_still_reports_saved(monkeypatch):
    stub_persistence(monkeypatch)

    def broken_format(record, max_length=None):
        raise TypeError("format_case_paragraph() got an unexpected keyword argument")

    monkeypatch.setattr(main, "format_case_paragraph", broken_format)
    result = main.update_case_record(base_record(), "新しい成果として応答時間が半減しました")
    assert result["ok"] is True
    assert result["action"] == "update"
    assert "保存まで完了" in result["slack_text"]


# --- 検索の関連度判定（2026-07-27 の本番障害の回帰テスト群） ---
#
# 障害条件: 「QRコード決済ゲートウェイを組み込んだアプリの事例はあるか」という
# 非AIテーマの質問に対し、Knowledge Base のベクトル検索が低スコア（0.78前後）の
# 無関係なAI事例を返し、Bot が「はい、あります」と断定して紹介してしまった。
# 本文・実名・実タイトルは含めず、障害と同じ条件だけを合成fixtureで再現する。


def unrelated_ai_search_records() -> list[dict]:
    """決済とは無関係な、登録済みAI事例の合成fixture。"""
    return [
        response_record(
            case_id="case-20260726000001-aaaa0001",
            title="店舗シフト表の自動作成ツール",
            summary="店長がAIの手を借りて、店舗のシフト表を自動で組むツールを作った社内PoCです。",
        ),
        response_record(
            case_id="case-20260726000002-aaaa0002",
            title="AI活用デモによる採用広報",
            summary="合同説明会でAI活用のライブデモを行い、採用広報につなげた取り組みです。",
        ),
        response_record(
            case_id="case-20260726000003-aaaa0003",
            title="売場ノウハウ集の構築",
            summary="ベテラン店員の売場づくりのコツを集め、店舗間で共有できるようにした事例です。",
        ),
    ]


def arrange_search_context(
    monkeypatch,
    records: list[dict],
    retrieval_results: list[dict],
    payload_text: str,
) -> None:
    """search_cases_tool を AWS 非接続で呼ぶための共通セットアップ。"""
    monkeypatch.setattr(main, "KNOWLEDGE_BASE_ID", "")
    monkeypatch.setattr(main, "scan_active_cases", lambda: records)
    monkeypatch.setattr(main, "save_search_state", lambda query, results: None)
    main._current_payload.set({"text": payload_text})
    main._tool_calls.set([])
    main._tool_results.set([])
    retrieved_ids: list[str] = []
    for item in retrieval_results:
        for case_id in item.get("case_ids", []):
            if case_id not in retrieved_ids:
                retrieved_ids.append(case_id)
    main._retrieved_case_ids.set(retrieved_ids)
    main._retrieval_results.set(retrieval_results)


def test_payment_gateway_question_does_not_claim_unrelated_ai_cases_exist(monkeypatch):
    """障害再現: 低スコアの無関係事例しか無いのに「はい、あります」と断定しない。

    足切り未満（0.75〜0.80）の候補は、関連度が高くないと明示したヒントとして
    最大2件だけ添える（該当ゼロ回答ばかりで有用性が下がるのを防ぐ）。
    """
    records = unrelated_ai_search_records()
    retrieval_results = [
        {"case_ids": [record["case_id"]], "score": score, "excerpt": record["summary"]}
        for record, score in zip(records, [0.7932, 0.7906, 0.7787])
    ]
    arrange_search_context(
        monkeypatch,
        records,
        retrieval_results,
        "モバイルアプリでQRコード決済を組み込みたいです。決済GWを組み込んだ開発の実績はありますか？",
    )

    result = json.loads(
        main.search_cases_tool(
            "モバイルアプリ QRコード決済 決済ゲートウェイ",
            ["QRコード決済", "決済代行", "スマホ決済"],
        )
    )

    text = result["slack_text"]
    assert "はい、あります" not in text
    assert "が近そうです" not in text
    assert "見つかりませんでした" in text
    # ヒントは関連度が高くないと明示したうえで、スコア上位2件だけ
    assert "関連度はそこまで高くありません" in text
    assert records[0]["title"] in text
    assert records[1]["title"] in text
    assert records[2]["title"] not in text
    assert not result.get("graphics")


def test_far_matches_are_not_offered_even_as_hints(monkeypatch):
    """ヒント下限（0.75）未満しか無ければ、候補は一切出さず該当なしで返す。"""
    records = unrelated_ai_search_records()
    retrieval_results = [
        {"case_ids": [record["case_id"]], "score": 0.70, "excerpt": record["summary"]}
        for record in records
    ]
    arrange_search_context(
        monkeypatch,
        records,
        retrieval_results,
        "モバイルアプリでQRコード決済を組み込みたいです。決済GWを組み込んだ開発の実績はありますか？",
    )

    result = json.loads(
        main.search_cases_tool(
            "モバイルアプリ QRコード決済 決済ゲートウェイ",
            ["QRコード決済", "決済代行"],
        )
    )

    text = result["slack_text"]
    assert "見つかりませんでした" in text
    assert "ヒントになる可能性" not in text
    for record in records:
        assert record["title"] not in text


def test_hint_candidates_are_saved_for_follow_up_questions(monkeypatch):
    """ヒント提示した候補は thread state に保存し、スレッドの追質問につなげる。"""
    records = unrelated_ai_search_records()
    retrieval_results = [
        {"case_ids": [record["case_id"]], "score": score, "excerpt": record["summary"]}
        for record, score in zip(records, [0.7932, 0.7906, 0.7787])
    ]
    saved_states: list[list[str]] = []
    arrange_search_context(
        monkeypatch,
        records,
        retrieval_results,
        "モバイルアプリでQRコード決済を組み込みたいです。決済GWを組み込んだ開発の実績はありますか？",
    )
    monkeypatch.setattr(
        main,
        "save_search_state",
        lambda query, results: saved_states.append([r["case_id"] for r in results]),
    )

    json.loads(
        main.search_cases_tool(
            "モバイルアプリ QRコード決済 決済ゲートウェイ",
            ["QRコード決済"],
        )
    )

    assert saved_states[-1] == [records[0]["case_id"], records[1]["case_id"]]


def test_non_ai_topic_search_explains_collection_scope(monkeypatch):
    """非AIテーマの質問には「AI事例収集Botなので未登録の可能性が高い」と案内する。"""
    records = unrelated_ai_search_records()
    retrieval_results = [
        {"case_ids": [record["case_id"]], "score": 0.79, "excerpt": record["summary"]}
        for record in records
    ]
    arrange_search_context(
        monkeypatch,
        records,
        retrieval_results,
        "モバイルアプリでQRコード決済を組み込みたいです。決済GWを組み込んだ開発の実績はありますか？",
    )

    result = json.loads(
        main.search_cases_tool(
            "モバイルアプリ QRコード決済 決済ゲートウェイ",
            ["QRコード決済", "決済代行"],
        )
    )

    text = result["slack_text"]
    assert "AI" in text
    assert "登録されていない可能性" in text


def test_weak_semantic_match_is_hedged_not_asserted(monkeypatch):
    """しきい値ぎりぎりの一致は「はい、あります」ではなくヘッジ表現で返す。"""
    record = response_record(
        title="問い合わせ対応の生成AIチャットボット",
        summary="社内ヘルプデスクの問い合わせ対応を生成AIチャットボットで効率化した事例です。",
    )
    retrieval_results = [
        {"case_ids": [record["case_id"]], "score": 0.81, "excerpt": record["summary"]}
    ]
    arrange_search_context(
        monkeypatch,
        [record],
        retrieval_results,
        "コールセンター業務を効率化したAI活用の実績はありますか？",
    )

    result = json.loads(main.search_cases_tool("コールセンター業務の効率化", []))

    text = result["slack_text"]
    assert "はい、あります" not in text
    assert f"「{record['title']}」" in text
    # AI関連の質問なので、収集スコープの案内は出さない
    assert "登録されていない可能性" not in text
    # 断定できない一致にはグラレコ画像を添付しない
    assert not result.get("graphics")


def test_confident_semantic_match_still_answers_affirmatively(monkeypatch):
    """高スコアのセマンティック一致は従来どおり「はい、あります」と答える。"""
    record = response_record(
        title="問い合わせ対応の生成AIチャットボット",
        summary="社内ヘルプデスクの問い合わせ対応を生成AIチャットボットで効率化した事例です。",
    )
    retrieval_results = [
        {"case_ids": [record["case_id"]], "score": 0.85, "excerpt": record["summary"]}
    ]
    arrange_search_context(
        monkeypatch,
        [record],
        retrieval_results,
        "コールセンター業務を効率化したAI活用の実績はありますか？",
    )

    result = json.loads(main.search_cases_tool("コールセンター業務の効率化", []))

    text = result["slack_text"]
    assert text.startswith(f"はい、あります。「{record['title']}」が近そうです。")


def test_confident_match_is_promoted_over_weak_semantic_hit(monkeypatch):
    """検索結果の並びで弱い一致が先頭でも、確信できる一致を主役に昇格させる。"""
    weak = response_record(
        case_id="case-20260726000011-bbbb0001",
        title="店舗会議のふりかえり支援AI",
        summary="店舗の週次会議のふりかえりをAIが要約して支援する社内実践です。",
    )
    confident = response_record(
        case_id="case-20260726000012-bbbb0002",
        title="問い合わせ対応の生成AIチャットボット",
        summary="社内ヘルプデスクの問い合わせ対応を生成AIチャットボットで効率化した事例です。",
    )
    retrieval_results = [
        {"case_ids": [weak["case_id"]], "score": 0.805, "excerpt": weak["summary"]},
        {"case_ids": [confident["case_id"]], "score": 0.86, "excerpt": confident["summary"]},
    ]
    arrange_search_context(
        monkeypatch,
        [weak, confident],
        retrieval_results,
        "コールセンター業務を効率化したAI活用の実績はありますか？",
    )

    result = json.loads(main.search_cases_tool("コールセンター業務の効率化", []))

    text = result["slack_text"]
    assert text.startswith(f"はい、あります。「{confident['title']}」が近そうです。")
    assert f"ほかには「{weak['title']}」" in text


def test_ai_topic_search_without_hits_keeps_rephrase_hint(monkeypatch):
    """AI関連の質問で見つからない場合は、スコープ案内ではなく言い換えを促す。"""
    arrange_search_context(
        monkeypatch,
        unrelated_ai_search_records(),
        [],
        "製造業向けの生成AI異常検知の実績はありますか？",
    )

    result = json.loads(main.search_cases_tool("製造業向け 生成AI 異常検知", ["異常検知"]))

    text = result["slack_text"]
    assert "登録されていない可能性" not in text
    assert "聞き方を変える" in text


def test_ai_topic_detection_for_scope_note():
    assert main.looks_like_ai_related_topic("生成AIで議事録を要約したい") is True
    assert main.looks_like_ai_related_topic("RAG構成のチャットボット") is True
    assert main.looks_like_ai_related_topic("AI活用の実績") is True
    assert main.looks_like_ai_related_topic("Bedrockのマルチエージェント") is True
    assert main.looks_like_ai_related_topic("QRコード決済のゲートウェイを組み込みたい") is False
    assert main.looks_like_ai_related_topic("勤怠管理システムの実績") is False
    # ASCII 単語の一部に ai / claude が含まれても誤検知しない
    assert main.looks_like_ai_related_topic("emailのdetailを確認したい") is False


def registered_case_reply() -> dict:
    """登録後の返信を再現する回帰テスト用データ。"""
    return {
        "ok": True,
        "action": "register",
        "case_id": "case-20260101000000-e0a10101",
        "slack_text": (
            "事例として登録しました。「店舗向け在庫照会アプリのリリース支援」として、"
            "社内共有用にはこう整理しています。\n\n"
            "社内のアプリ開発チームが進行管理を担当し、店舗向け在庫照会アプリのリリースを支援した事例です。\n\n"
            f"{main.GRAPHIC_PENDING_NOTICE}"
        ),
    }


def run_chat_reply_with_prior(prior: list[dict], message: str) -> tuple[dict, list[dict]]:
    payload_token = main._current_payload.set({"text": "URL読んで事例登録しといて"})
    tool_calls_token = main._tool_calls.set(["retrieve_cases_tool", "register_case_tool", "chat_reply_tool"])
    tool_results_token = main._tool_results.set(list(prior))
    try:
        result = json.loads(main.chat_reply_tool(message=message))
        tracked = list(main._tool_results.get())
    finally:
        main._tool_results.reset(tool_results_token)
        main._tool_calls.reset(tool_calls_token)
        main._current_payload.reset(payload_token)
    return result, tracked


def test_chat_reply_cannot_overwrite_single_registration_with_placeholder():
    """2026-08-03 障害の回帰テスト。

    登録は成功したのに、モデルが追加で chat_reply_tool を呼び 'placeholder' を渡したため、
    Slack へ 'placeholder' だけが投稿された。1件だけの登録・更新は上書きさせない。
    """
    prior = registered_case_reply()

    result, tracked = run_chat_reply_with_prior([prior], "placeholder")

    assert result == prior
    assert "placeholder" not in result["slack_text"]
    assert tracked == [prior]


def test_chat_reply_cannot_overwrite_single_registration_even_with_natural_text():
    """まともな文であっても、1件だけの登録の完成返信を差し替えさせない。"""
    prior = registered_case_reply()

    result, _ = run_chat_reply_with_prior([prior], "登録しておきました！")

    assert result == prior


def test_chat_reply_rejects_placeholder_in_multi_case_summary():
    """複数事例のまとめ報告は許可するが、穴埋め文字列なら直前の登録結果へ戻す。"""
    first = {**registered_case_reply(), "case_id": "case-a"}
    second = {**registered_case_reply(), "case_id": "case-b"}

    result, _ = run_chat_reply_with_prior([first, second], "Placeholder.")

    assert result == second


def test_chat_reply_allows_real_multi_case_summary():
    """2件以上を登録した後の正当なまとめ報告は今まで通り通す。"""
    first = {**registered_case_reply(), "case_id": "case-a"}
    second = {**registered_case_reply(), "case_id": "case-b"}

    result, tracked = run_chat_reply_with_prior(
        [first, second],
        "2件を登録しました。「店舗向け在庫照会アプリのリリース支援」と「社内問い合わせ検索基盤」です。",
    )

    assert result["action"] == "chat"
    assert "2件を登録しました" in result["slack_text"]
    assert len(tracked) == 3


def test_chat_reply_placeholder_without_prior_result_returns_recoverable_message():
    result, _ = run_chat_reply_with_prior([], "placeholder")

    assert result["ok"] is False
    assert "placeholder" not in result["slack_text"]
    assert "もう一度メンション" in result["slack_text"]


def test_placeholder_reply_detection_is_conservative():
    assert main.looks_like_placeholder_reply("placeholder") is True
    assert main.looks_like_placeholder_reply("  Placeholder.  ") is True
    assert main.looks_like_placeholder_reply("TBD") is True
    assert main.looks_like_placeholder_reply("（ここに本文）") is True
    assert main.looks_like_placeholder_reply("") is True
    assert main.looks_like_placeholder_reply("   ") is True
    # 正当な短い返信を誤って捨てない
    assert main.looks_like_placeholder_reply("登録しました！") is False
    assert main.looks_like_placeholder_reply("はい、あります。") is False
    assert main.looks_like_placeholder_reply("URLは会員限定で読めませんでした。") is False
    assert main.looks_like_placeholder_reply("placeholderの扱いについて説明しますね。") is False


# --- グラレコ要求の回帰テスト -------------------------------------------------
# 2026-08-04、スレッドで「グラレコできた？」と聞かれた Bot が、確認できるはずの
# graphic_status を見に行かず「生成は開始されているはずです」と推測し、そのうえ
# 投稿されることのないチャンネルを利用者に見に行かせる返信を出した。
# 同じ条件を再生し、画像そのものを返すことを確認する。


def graphic_request_payload(text: str = "グラレコできた？") -> dict:
    return {
        "mode": "slack_app_mention",
        "text": text,
        "channel_id": "C0123456789",
        "thread_ts": "1754300000.000100",
    }


def run_graphic_tool(monkeypatch, record: dict, text: str = "グラレコできた？") -> dict:
    monkeypatch.setattr(main, "cases_table", FakeCaseLookupTable(record))
    monkeypatch.setattr(main, "save_thread_state", lambda _record: None)
    payload_token = main._current_payload.set(graphic_request_payload(text))
    tool_calls_token = main._tool_calls.set([])
    tool_results_token = main._tool_results.set([])
    try:
        return json.loads(main.get_case_graphic_tool(case_id=record["case_id"]))
    finally:
        main._tool_results.reset(tool_results_token)
        main._tool_calls.reset(tool_calls_token)
        main._current_payload.reset(payload_token)


def assert_does_not_defer_to_user(slack_text: str) -> None:
    """確認できることを推測で答えたり、利用者に確認を押し付けたりしていないこと。"""
    for phrase in ["はずです", "チャンネルの投稿", "確認できない", "確認できません"]:
        assert phrase not in slack_text


def test_graphic_request_returns_the_image_when_ready(monkeypatch):
    record = response_record(title="みどり書房 発注予測AI導入支援")
    result = run_graphic_tool(monkeypatch, record)

    assert result["ok"] is True
    assert result["action"] == "graphic"
    assert result["graphic_s3_key"] == record["graphic_s3_key"]
    assert len(result["graphics"]) == 1
    assert "みどり書房 発注予測AI導入支援" in result["slack_text"]
    assert_does_not_defer_to_user(result["slack_text"])


def test_graphic_request_reports_pending_with_eta(monkeypatch):
    record = response_record(
        graphic_status="pending",
        graphic_due_epoch=main.epoch_seconds() + 180,
    )
    record.pop("graphic_s3_key")
    result = run_graphic_tool(monkeypatch, record)

    assert result["ok"] is True
    assert result["action"] == "graphic"
    assert "graphics" not in result
    assert "3分" in result["slack_text"]
    assert_does_not_defer_to_user(result["slack_text"])


def test_graphic_request_reschedules_when_generation_failed(monkeypatch):
    record = response_record(graphic_status="failed")
    record.pop("graphic_s3_key")
    scheduled: list[str] = []

    def fake_schedule(target: dict) -> dict:
        scheduled.append(target["case_id"])
        return {**target, "graphic_status": "pending"}

    monkeypatch.setattr(main, "schedule_case_graphic", fake_schedule)
    result = run_graphic_tool(monkeypatch, record)

    assert scheduled == [record["case_id"]]
    assert result["action"] == "graphic"
    assert "作り直し" in result["slack_text"]
    assert_does_not_defer_to_user(result["slack_text"])


def test_graphic_request_is_routed_without_model_tool_choice(monkeypatch):
    """モデルのツール選択に頼らず、コード側でグラレコ要求を確定させる。"""
    record = response_record(title="みどり書房 発注予測AI導入支援")
    monkeypatch.setattr(main, "cases_table", FakeCaseLookupTable(record))
    monkeypatch.setattr(
        main,
        "thread_state_table",
        FakeThreadStateLookupTable({"thread_key": "C0123456789#1754300000.000100", "case_id": record["case_id"]}),
    )
    monkeypatch.setattr(main, "save_thread_state", lambda _record: None)
    tool_calls_token = main._tool_calls.set([])
    tool_results_token = main._tool_results.set([])
    payload_token = main._current_payload.set(graphic_request_payload())
    try:
        result = main.handle_case_graphic_request(graphic_request_payload())
    finally:
        main._current_payload.reset(payload_token)
        main._tool_results.reset(tool_results_token)
        main._tool_calls.reset(tool_calls_token)

    assert result is not None
    assert result["action"] == "graphic"
    assert result["graphic_s3_key"] == record["graphic_s3_key"]


def test_graphic_request_detection_covers_natural_phrasing():
    for text in [
        "グラレコできた？",
        "グラレコある？",
        "グラレコあるの？",
        "グラレコ見せて",
        "グラレコまだ？",
        "さっきの事例のグラレコ、もう出来上がった？",
        "グラレコちょうだい",
    ]:
        assert main.is_case_graphic_request(text) is True, text

    # このBot自身の作り方を尋ねる設計質問は対象外
    for text in [
        "君のグラレコはどうやって作ってるの？",
        "このBotのグラレコの文字化け対策は？",
        "グラレコの仕組みを教えて",
    ]:
        assert main.is_case_graphic_request(text) is False, text


def test_graphic_request_wins_over_bot_design_routing():
    """設計質問が先行するスレッドでも、画像要求は設計説明に吸われない。"""
    thread_messages = [{"text": "このBotのグラレコはどう作ってるの？", "user": "U1"}]

    assert main.is_bot_design_question("グラレコできた？", thread_messages) is False
    assert main.is_bot_design_question("このBotのグラレコはどう作ってるの？", thread_messages) is True


# --- 更新先はLLMが case_id で選ぶ（コードは点数・引用符で決めない） ---
# 利用者が二重カギカッコ付きの事例名で返しても、LLMが選んだ case_id へ書く。

DIGEST = {"case_id": "case-digest", "title": "「店舗日報ダイジェスト」による多店舗チェーンの売場の課題の可視化", "status": "ready", "summary": "売場日報の記録から店舗ごとの課題を集計"}
WAREHOUSE = {"case_id": "case-warehouse", "title": "〇〇物流の倉庫DX案件でのテスト自動化と効果測定", "status": "ready", "summary": "テスト自動化を導入し効果を測定"}
BUFFER_TEXT = "売場日報の集計で欠品の多い時間帯を特定し、発注の締め時刻を1時間早めた。12店舗・8週分を集計した。"
DOUBLE_BRACKET_REPLY = "「「店舗日報ダイジェスト」による多店舗チェーンの売場の課題の可視化」に追記して"


class FakeThreadStateTable:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def put_item(self, **kwargs):
        self.items.append(kwargs["Item"])
        return {}


def stub_target_world(monkeypatch, state, records=(DIGEST, WAREHOUSE)):
    fake = stub_persistence(monkeypatch)
    by_id = {record["case_id"]: record for record in records}
    monkeypatch.setattr(main, "get_case_by_id", lambda case_id: by_id.get(case_id))
    monkeypatch.setattr(main, "get_cases_by_ids", lambda case_ids: [by_id[c] for c in case_ids if c in by_id])
    monkeypatch.setattr(main, "get_current_thread_state", lambda payload: state)
    threads = FakeThreadStateTable()
    monkeypatch.setattr(main, "thread_state_table", threads)
    return fake, threads


def run_with(text, fn, intent=("append", 0.95), retrieved=()):
    payload = {"mode": "slack_app_mention", "text": text, "channel_id": "C1", "thread_ts": "1.0", "message_ts": "2.0"}
    tokens = [
        (main._current_payload, main._current_payload.set(payload)),
        (main._intent, main._intent.set({"intent": intent[0], "confidence": intent[1]} if intent else {})),
        (main._retrieved_case_ids, main._retrieved_case_ids.set(list(retrieved))),
        (main._tool_results, main._tool_results.set([])),
    ]
    try:
        return json.loads(fn())
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def pending_state(**extra):
    return {
        "pending_action": "update_case",
        "additional_content": BUFFER_TEXT,
        "candidate_case_ids": [DIGEST["case_id"], WAREHOUSE["case_id"]],
        "candidate_titles": [DIGEST["title"], WAREHOUSE["title"]],
        "updated_at": main.utc_now_iso(),
        **extra,
    }


def written_case_id(fake):
    return fake.last_update_kwargs["Key"]["case_id"] if fake.last_update_kwargs else None


def test_double_bracket_title_reply_updates_case_chosen_by_llm(monkeypatch):
    fake, _ = stub_target_world(monkeypatch, pending_state())
    result = run_with(DOUBLE_BRACKET_REPLY, lambda: main.confirm_pending_update_tool(selection=DOUBLE_BRACKET_REPLY, case_id=DIGEST["case_id"]))
    assert result["action"] == "update"
    assert written_case_id(fake) == DIGEST["case_id"]
    # 書くのは「〜に追記して」の一文ではなく、聞き返しの前に預かった本文
    assert "8週分" in json.dumps(fake.last_update_kwargs, ensure_ascii=False)
    assert "に追記して" not in fake.last_update_kwargs["ExpressionAttributeValues"][":summary"]


def test_pending_reply_without_case_id_asks_again_instead_of_guessing(monkeypatch):
    fake, threads = stub_target_world(monkeypatch, pending_state())
    result = run_with(DOUBLE_BRACKET_REPLY, lambda: main.confirm_pending_update_tool(selection=DOUBLE_BRACKET_REPLY))
    assert result["action"] == "update_pending"
    assert fake.last_update_kwargs is None
    assert threads.items[-1]["additional_content"] == BUFFER_TEXT


def test_pending_number_selection_still_works(monkeypatch):
    fake, _ = stub_target_world(monkeypatch, pending_state())
    result = run_with("2番に追記して", lambda: main.update_case_tool(additional_content="2番に追記して"))
    assert result["action"] == "update"
    assert written_case_id(fake) == WAREHOUSE["case_id"]


def test_llm_chosen_case_overrides_thread_case(monkeypatch):
    fake, _ = stub_target_world(monkeypatch, {"case_id": WAREHOUSE["case_id"]})
    run_with("店舗日報ダイジェストの方に追記して。本部の会議にも出した", lambda: main.update_case_tool(additional_content="本部の会議にも出した", case_id=DIGEST["case_id"]))
    assert written_case_id(fake) == DIGEST["case_id"]


def test_thread_case_is_default_target(monkeypatch):
    fake, _ = stub_target_world(monkeypatch, {"case_id": DIGEST["case_id"]})
    run_with("この事例に追記して。本部の会議にも出した", lambda: main.update_case_tool(additional_content="本部の会議にも出した"))
    assert written_case_id(fake) == DIGEST["case_id"]


def test_unknown_target_asks_by_number_and_keeps_content(monkeypatch):
    fake, threads = stub_target_world(monkeypatch, None)
    result = run_with(
        "この内容を追記しておいて。問い合わせの件数が3割減った",
        lambda: main.update_case_tool(additional_content="問い合わせの件数が3割減った"),
        retrieved=[DIGEST["case_id"], WAREHOUSE["case_id"]],
    )
    assert result["action"] == "update_pending"
    assert fake.last_update_kwargs is None
    assert "1番" in result["slack_text"] and "case-" not in result["slack_text"]
    assert "で囲んで" not in result["slack_text"]
    assert threads.items[-1]["additional_content"] == "問い合わせの件数が3割減った"
    assert threads.items[-1]["candidate_case_ids"] == [DIGEST["case_id"], WAREHOUSE["case_id"]]


def test_nonexistent_case_id_is_not_written(monkeypatch):
    fake, _ = stub_target_world(monkeypatch, None)
    result = run_with("追記して", lambda: main.update_case_tool(additional_content="効果が出た", case_id="case-does-not-exist"), retrieved=[DIGEST["case_id"]])
    assert result["action"] == "update_pending"
    assert fake.last_update_kwargs is None


def test_ask_register_or_update_keeps_request_for_append_answer(monkeypatch):
    _, threads = stub_target_world(monkeypatch, None)
    result = run_with(
        BUFFER_TEXT + " 登録してください",
        lambda: main.ask_register_or_update_tool(candidate_case_ids=[DIGEST["case_id"], WAREHOUSE["case_id"]], message="売場日報の集計という点で、店舗日報ダイジェストの事例と重なって見えます。"),
        intent=("register", 0.9),
    )
    assert result["action"] == "register_or_update_question"
    assert result["slack_text"].startswith("売場日報の集計という点で")
    assert "1番に追記して" in result["slack_text"] and "別件として登録して" in result["slack_text"]
    assert "で囲んで" not in result["slack_text"]
    assert threads.items[-1]["pending_action"] == "update_case"
    assert "8週分" in threads.items[-1]["additional_content"]


def test_stale_pending_is_not_used(monkeypatch):
    stale = pending_state(updated_at="2026-01-01T00:00:00Z")
    fake, _ = stub_target_world(monkeypatch, stale)
    run_with("店舗日報ダイジェストに追記して。本部の会議にも出した", lambda: main.update_case_tool(additional_content="本部の会議にも出した", case_id=DIGEST["case_id"]))
    assert written_case_id(fake) == DIGEST["case_id"]
    assert "8週分" not in json.dumps(fake.last_update_kwargs, ensure_ascii=False)


def test_pending_save_keeps_thread_case(monkeypatch):
    _, threads = stub_target_world(monkeypatch, {"case_id": DIGEST["case_id"]})
    run_with("x", lambda: json.dumps(main.build_update_target_question([WAREHOUSE], "内容")))
    assert threads.items[-1]["case_id"] == DIGEST["case_id"]


def test_prompt_state_lists_numbered_candidates():
    summary = main.summarize_thread_state(pending_state())
    assert summary["candidates"][0] == {"number": 1, "case_id": DIGEST["case_id"], "title": DIGEST["title"]}
    assert "8週分" in summary["pending_content_excerpt"]


def test_chat_reply_proposes_target_and_repends_single_candidate(monkeypatch):
    """追記先の見当がついたら、事例名の打ち直しを頼まずに預け直して「お願い」で追記できるようにする。"""
    fake, threads = stub_target_world(monkeypatch, pending_state(candidate_case_ids=["case-x"], candidate_titles=["無関係"]))
    result = run_with(
        "改修したよ。どうすべきか改めて考えてみて！",
        lambda: main.chat_reply_tool(message="鈴木さんのご希望は「店舗日報ダイジェスト」の事例への追記でした。", propose_case_id=DIGEST["case_id"]),
        intent=("chat", 0.8),
    )
    assert result["proposed_case_id"] == DIGEST["case_id"]
    assert "「お願い」と返信" in result["slack_text"]
    assert fake.last_update_kwargs is None
    assert threads.items[-1]["candidate_case_ids"] == [DIGEST["case_id"]]
    assert threads.items[-1]["additional_content"] == BUFFER_TEXT


def test_chat_proposal_does_not_repeat_how_to_reply(monkeypatch):
    stub_target_world(monkeypatch, pending_state())
    message = "追記先は「店舗日報ダイジェスト」の事例だと思います。よければ「お願いします」と返信してください。"
    result = run_with("考えてみて", lambda: main.chat_reply_tool(message=message, propose_case_id=DIGEST["case_id"]), intent=("chat", 0.8))
    assert result["slack_text"] == message
    assert result["proposed_case_id"] == DIGEST["case_id"]


def test_agreement_after_proposal_appends_pending_content(monkeypatch):
    state = pending_state(candidate_case_ids=[DIGEST["case_id"]], candidate_titles=[DIGEST["title"]])
    fake, _ = stub_target_world(monkeypatch, state)
    result = run_with("お願い", lambda: main.confirm_pending_update_tool(selection="お願い"))
    assert result["action"] == "update"
    assert written_case_id(fake) == DIGEST["case_id"]
    assert "8週分" in json.dumps(fake.last_update_kwargs, ensure_ascii=False)


def test_chat_proposal_without_pending_does_nothing(monkeypatch):
    _, threads = stub_target_world(monkeypatch, None)
    result = run_with("ありがとう", lambda: main.chat_reply_tool(message="どういたしまして！", propose_case_id=DIGEST["case_id"]), intent=("chat", 0.9))
    assert result["slack_text"] == "どういたしまして！"
    assert threads.items == []


def test_no_regex_or_score_decides_update_target():
    for name in ("find_cases_by_quoted_title", "should_auto_update", "extract_update_target_hint", "resolve_update_target_and_update"):
        assert not hasattr(main, name), name


# --- 1依頼で2事例を更新しない / 別顧客の内容で上書きしない / 版履歴 / 混在カードの配信停止 ---


def sample_record() -> dict:
    return {
        "case_id": "case-20260101000000-sample01",
        "title": "みどり書房さま 店舗の発注予測AI（Shelf Forecast）導入支援",
        "summary": "みどり書房さま向けに、店舗の発注予測AI「Shelf Forecast」の要件整理から検証、店舗展開までを伴走支援した事例",
        "project_or_customer": "みどり書房",
        "status": "ready",
        "sensitive_flags": [],
        "keywords": [],
    }


OTHER_CUSTOMER_KICKOFF_TEXT = (
    "あおば運輸様向けに、配車計画の最適化（Route Planner）の検証を始めた。"
    "社内メンバーは高橋さん、伊藤さん、渡辺さん、サポートの中村さんの体制。"
)


def test_customer_identity_token_strips_honorifics_and_project_names():
    assert main.customer_identity_token("あおば運輸様 配車計画最適化支援（Route Planner）") == "あおば運輸"
    assert main.customer_identity_token("株式会社みどり書房") == "みどり書房"
    assert main.customer_identity_token("AOBA(配車本部)") == "aoba"
    assert main.customer_identity_token("") == ""


def test_update_refuses_content_about_another_customer(monkeypatch):
    """別の会社の案件の本文が既存事例へ書き込まれそうなときは、書き込まず確認へ倒す。"""
    fake = stub_persistence(monkeypatch)
    result = main.update_case_record(
        sample_record(),
        OTHER_CUSTOMER_KICKOFF_TEXT,
        extracted_fields={"project_or_customer": "あおば運輸様 配車計画最適化支援（Route Planner）", "impact": "配車計画づくりの効率化に着手"},
    )
    assert result["ok"] is False
    assert result["action"] == "update_refused"
    assert result["reason"] == "customer_mismatch"
    assert "あおば運輸" in result["slack_text"] and "みどり書房" in result["slack_text"]
    assert fake.last_update_kwargs is None


def test_update_allows_same_customer_with_verbose_name(monkeypatch):
    fake = stub_persistence(monkeypatch)
    result = main.update_case_record(
        sample_record(),
        "みどり書房さまの Shelf Forecast は導入店舗が14店舗に増えました",
        extracted_fields={"project_or_customer": "みどり書房株式会社 Shelf Forecast 導入支援", "impact": "導入店舗が14店舗に増加"},
    )
    assert result["ok"] is True
    assert fake.last_update_kwargs is not None


def test_update_allows_explicit_customer_correction(monkeypatch):
    """顧客名そのものを直す依頼（ではなく／が正しい）は、別会社の名前でも通す。"""
    fake = stub_persistence(monkeypatch, bedrock_text="南北製パン向けにパンの焼き色検査をAIで自動化し、検査時間を短縮した事例。")
    record = sample_record()
    record["title"] = "東西製パン向け 焼き色検査AI"
    record["summary"] = "東西製パン向けにパンの焼き色検査をAIで自動化した事例"
    record["project_or_customer"] = "東西製パン"
    result = main.update_case_record(
        record,
        "導入先は東西製パンじゃなくて南北製パン",
        extracted_fields={"project_or_customer": "南北製パン", "summary": "導入先は東西製パンではなく南北製パンが正しい情報です"},
    )
    assert result["ok"] is True
    assert fake.last_update_kwargs is not None


def test_update_refuses_second_case_in_same_turn(monkeypatch):
    """検索で2件ヒットした事例の両方へ同じ追記を書かない。2件目は拒否する。"""
    fake = stub_persistence(monkeypatch)
    token = main._tool_results.set([{"ok": True, "action": "update", "case_id": "case-aoba", "title": "あおば運輸さま 配車計画最適化支援"}])
    try:
        result = main.update_case_record(sample_record(), "みどり書房さまの Shelf Forecast に追記です")
    finally:
        main._tool_results.reset(token)
    assert result["ok"] is False
    assert result["action"] == "update_refused"
    assert result["reason"] == "multiple_targets"
    assert "あおば運輸さま 配車計画最適化支援" in result["slack_text"]
    assert fake.last_update_kwargs is None


def test_update_result_carries_title_for_second_target_guard(monkeypatch):
    stub_persistence(monkeypatch)
    result = main.update_case_record(sample_record(), "みどり書房さまの Shelf Forecast に追記です")
    assert result["ok"] is True
    assert result["title"] == sample_record()["title"]


def test_update_writes_revisions_before_case(monkeypatch):
    fake = stub_persistence(monkeypatch)
    result = main.update_case_record(sample_record(), "みどり書房さまの Shelf Forecast に追記です")
    assert result["ok"] is True
    kinds = [(item["kind"], item["revision"]) for item in fake.revisions.items]
    assert kinds == [("baseline", 0), ("update", 1)]
    assert fake.revisions.items[0]["record"]["summary"] == sample_record()["summary"]
    assert fake.last_update_kwargs["ExpressionAttributeValues"][":revision"] == 1
    assert "attribute_not_exists(revision)" in fake.last_update_kwargs["ConditionExpression"]


def test_update_of_versioned_case_checks_expected_revision(monkeypatch):
    fake = stub_persistence(monkeypatch)
    record = {**sample_record(), "revision": 3}
    main.update_case_record(record, "みどり書房さまの Shelf Forecast に追記です")
    values = fake.last_update_kwargs["ExpressionAttributeValues"]
    assert values[":revision"] == 4 and values[":expected_revision"] == 3
    assert [item["kind"] for item in fake.revisions.items] == ["update"]


def test_update_is_not_written_when_revision_write_fails(monkeypatch):
    fake = stub_persistence(monkeypatch)
    fake.revisions.fail = True
    result = main.update_case_record(sample_record(), "みどり書房さまの Shelf Forecast に追記です")
    assert result["ok"] is False
    assert result["owner_alert"] == "case_revision_write_failed"
    assert fake.last_update_kwargs is None
    assert "時間をおいて" not in result["slack_text"]


def test_revert_restores_previous_revision(monkeypatch):
    fake = stub_persistence(monkeypatch)
    current = {**sample_record(), "summary": "取り違えて上書きされた紹介文", "revision": 1}
    fake.revisions.items.append({"case_id": current["case_id"], "revision": 0, "kind": "baseline", "record": {k: v for k, v in sample_record().items() if k != "case_id"}})
    puts: list[dict] = []
    monkeypatch.setattr(main, "get_case_by_id", lambda case_id: current)
    monkeypatch.setattr(fake, "put_item", lambda **kwargs: puts.append(kwargs) or {}, raising=False)
    result = main.revert_case_to_revision(current["case_id"])
    assert result["ok"] is True
    assert puts[0]["Item"]["summary"] == sample_record()["summary"]
    assert puts[0]["Item"]["revision"] == 2
    assert fake.revisions.items[-1]["kind"] == "revert"


def test_daily_share_skips_mixed_record(monkeypatch):
    """顧客名がタイトル・紹介文と別会社のカードは日次共有に載せない。"""
    mixed = sample_record()
    mixed["project_or_customer"] = "あおば運輸様 配車計画最適化支援（Route Planner）"
    mixed["last_shared_at"] = "2026-07-01T00:00:00Z"
    clean = sample_record()
    clean["case_id"] = "case-clean"
    clean["last_shared_at"] = "2026-08-01T00:00:00Z"
    other = other_customer_record()
    other["last_shared_at"] = "2026-09-01T00:00:00Z"
    monkeypatch.setattr(main, "scan_active_cases", lambda: [mixed, clean, other])
    monkeypatch.setattr(main, "with_case_graphic", lambda result, record: result)
    token = main._tool_results.set([])
    try:
        result = json.loads(main.pick_daily_share_tool())
    finally:
        main._tool_results.reset(token)
    assert result["post_to_channel"] is True
    assert result["case_id"] == "case-clean"


def other_customer_record() -> dict:
    return {"case_id": "case-aoba", "title": "あおば運輸さま 配車計画最適化支援", "project_or_customer": "あおば運輸様 配車計画最適化支援", "status": "ready"}


def test_case_looks_mixed_detects_contaminated_card_even_if_summary_mentions_customer():
    """紹介文に別会社の追記が混ざり、顧客名も別会社になったカードは、紹介文に顧客名があっても混在と判定する。"""
    damaged = sample_record()
    damaged["project_or_customer"] = "あおば運輸様 配車計画最適化支援（Route Planner）"
    damaged["summary"] += " 追記: あおば運輸様向けに、配車計画の最適化（Route Planner）の検証を始めた。"
    assert main.case_looks_mixed(damaged, [damaged, other_customer_record()]) == damaged["project_or_customer"]


def test_case_looks_mixed_accepts_consistent_cards():
    others = [sample_record(), other_customer_record()]
    assert main.case_looks_mixed(sample_record(), others) == ""
    assert main.case_looks_mixed(other_customer_record(), others) == ""
    assert main.case_looks_mixed({"case_id": "c1", "title": "みどり書房向け 店員のClaude活用研修", "project_or_customer": "みどり書房(店舗運営本部)"}, others) == ""
    # 会社名＋製品名（みどり書房 Shelf Forecast）は、2語目がタイトルにあれば同じ案件
    product = {"case_id": "c4", "title": "Shelf Forecast（発注予測AI）の新機能追加", "project_or_customer": "みどり書房 Shelf Forecast"}
    same_company_other = {"case_id": "c5", "title": "みどり書房向け 店員のClaude活用研修", "project_or_customer": "みどり書房(店舗運営本部)"}
    assert main.case_looks_mixed(product, others + [product, same_company_other]) == ""
    # 社内プロジェクト名がタイトルと違う言い回しでも、他事例に同じ組織名が無ければ疑わない
    internal = {"case_id": "c2", "title": "社内展開AIエージェント「まかないアシスタント」", "project_or_customer": "こもれび食品株式会社「AIまかないくん」"}
    assert main.case_looks_mixed(internal, others + [internal]) == ""
    assert main.case_looks_mixed({"case_id": "c3", "title": "社内ツール", "project_or_customer": ""}, others) == ""


# --- 社内ラベル・部署名だけの顧客名は「混在」と判定しない ---
# 「社内」「po」「部署名」の一致だけで混在扱いにすると、正常な事例が日次共有から外れる。

MIXED_FALSE_POSITIVE_OTHERS = [
    {"case_id": "o1", "title": "E2E_TEST_XYZ取引先チェックシートの記入補助", "project_or_customer": "XYZ案件（社内共通）"},
    {"case_id": "o2", "title": "E2E_TEST_売場企画フレームワーク", "project_or_customer": "E2E_TEST_売場キャンバス（社内発フレームワーク）"},
    {"case_id": "o3", "title": "E2E_TEST_PO連絡帳のAI活用メモ", "project_or_customer": "PO連絡帳（店舗間）チーム AI活用メモ"},
    {"case_id": "o4", "title": "E2E_TEST_つばさ保険様 見積もりプロトタイプとPoC支援", "project_or_customer": "つばさ保険様"},
    {"case_id": "o5", "title": "E2E_TEST_契約書の簡易チェック", "project_or_customer": "管理本部 法務"},
    {"case_id": "o6", "title": "E2E_TEST_仕入れの請求書照合", "project_or_customer": "管理本部 購買"},
]
INTERNAL_LABEL_CASES = [
    ("i1", "E2E_TEST_社内ヘルプデスクのClaude活用", "社内（情報システム）"),
    ("i2", "E2E_TEST_既存システムの引き継ぎ資料", "社内（保険系案件 つばさ）"),
    ("i3", "E2E_TEST_来店客アンケートのナレッジ基盤", "社内（販売企画部）"),
    ("i4", "E2E_TEST_返品対応の漏れ防止アラート", "管理本部"),
    ("i5", "E2E_TEST_仕入れ先チェックの一次スクリーニング", "社内（各店舗）"),
    ("i6", "E2E_TEST_店長がシフト管理ツールを自作", "PM業務ツール自作（シフト・在庫管理）"),
    ("i7", "E2E_TEST_交通費精算の自動チェック", "社内（交通費精算）"),
]


@pytest.mark.parametrize("case_id,title,customer", INTERNAL_LABEL_CASES)
def test_case_looks_mixed_ignores_internal_labels(case_id, title, customer):
    record = {"case_id": case_id, "title": title, "project_or_customer": customer}
    assert main.case_looks_mixed(record, MIXED_FALSE_POSITIVE_OTHERS + [record]) == ""


def test_case_looks_mixed_still_detects_real_mix_among_internal_cards():
    """社内ラベルの事例が並んでいても、別会社の内容が混ざったカードは検出し続ける。"""
    damaged = sample_record()
    damaged["project_or_customer"] = "あおば運輸様 配車計画最適化支援（Route Planner）"
    internal = [{"case_id": c, "title": t, "project_or_customer": p} for c, t, p in INTERNAL_LABEL_CASES]
    records = MIXED_FALSE_POSITIVE_OTHERS + internal + [damaged, other_customer_record()]
    assert main.case_looks_mixed(damaged, records) == damaged["project_or_customer"]
    assert [r["case_id"] for r in records if main.case_looks_mixed(r, records)] == [damaged["case_id"]]


# --- 「登録して」を更新へ回さない ---

CLOSING_RECORD = {
    "case_id": "case-20260201000000-sample02",
    "title": "経理の月次締め作業の自動化5本で月24時間削減",
    "summary": "経理部が月次締めの定型作業を減らすためClaudeで5本の自動化を構築",
    "approach": "仕訳チェック、請求書の突き合わせ、売上日報の集計、店舗日報ダイジェストなど5本を自動化",
    "status": "ready",
}
REGISTER_TEXT = (
    "販売企画部の、多店舗チェーン向け「店舗日報ダイジェスト」の事例です。登録してください\n"
    "・AIの使い方: 売場日報の記録をもとに、店舗ごとの欠品や品出しの課題を整理"
)


def test_update_tool_asks_register_or_update_when_user_asked_to_register(monkeypatch):
    fake = stub_persistence(monkeypatch)
    monkeypatch.setattr(main, "get_current_thread_state", lambda payload: None)
    monkeypatch.setattr(main, "rank_cases", lambda query, search_terms=None: [{"record": CLOSING_RECORD, "score": 32}])
    token = main._current_payload.set({"mode": "slack_app_mention", "text": REGISTER_TEXT})
    try:
        result = json.loads(main.update_case_tool(additional_content=REGISTER_TEXT, title="店舗日報ダイジェスト"))
    finally:
        main._current_payload.reset(token)
    assert result["action"] == "register_or_update_question"
    assert fake.last_update_kwargs is None
    assert "新しい事例として登録するか" in result["slack_text"]
    assert "別件として登録して" in result["slack_text"]
    assert "経理の月次締め作業の自動化5本で月24時間削減" in result["slack_text"]
    assert "更新したい内容は預かりました" not in result["slack_text"]


def test_explicit_update_request_is_not_redirected():
    assert main.is_register_without_update_request(REGISTER_TEXT) is True
    assert main.is_register_without_update_request("「経理の月次締め作業の自動化5本で月24時間削減」に店舗日報ダイジェストを追記して") is False
    assert main.is_register_without_update_request("この事例を更新して、登録内容を直して") is False


def test_ask_register_or_update_tool_lists_candidates(monkeypatch):
    stub_persistence(monkeypatch)
    monkeypatch.setattr(main, "get_case_by_id", lambda case_id: CLOSING_RECORD if case_id == CLOSING_RECORD["case_id"] else None)
    result = json.loads(main.ask_register_or_update_tool(candidate_case_ids=[CLOSING_RECORD["case_id"], "case-missing"]))
    assert result["action"] == "register_or_update_question"
    assert [c["case_id"] for c in result["candidates"]] == [CLOSING_RECORD["case_id"]]


def test_prompt_forbids_switching_register_to_update():
    assert "ask_register_or_update_tool" in main.SYSTEM_PROMPT
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert "明確に同一の取り組みが既にあれば register_case_tool ではなく update_case_tool" not in source
    assert "利用者が「登録して」と依頼しているのに update_case_tool を選んではいけません" in source


def test_register_request_mentioning_claude_and_bot_is_not_bot_design_question():
    text = (
        "Claudeに手伝ってもらって作った「棚割りチェッカー」の事例です。"
        "Botの使い方の質問ではないので、新しい事例として登録してください。"
    )
    assert main.is_bot_design_question(text) is False
    assert main.is_bot_design_question("このBotのモデルは何を使っていますか？") is True



def test_write_gate_follows_classified_intent():
    token = main._intent.set({"intent": "chat", "confidence": 0.9})
    try:
        assert main.write_allowed("append") is False
    finally:
        main._intent.reset(token)
    token = main._intent.set({"intent": "append", "confidence": 0.5})
    try:
        assert main.write_allowed("append") is False
    finally:
        main._intent.reset(token)
    token = main._intent.set({"intent": "append", "confidence": 0.9})
    try:
        assert main.write_allowed("append") is True
    finally:
        main._intent.reset(token)


def test_update_tool_asks_instead_of_writing_when_intent_is_not_append(monkeypatch):
    fake = stub_persistence(monkeypatch)
    monkeypatch.setattr(main, "get_current_thread_state", lambda payload: {"case_id": CLOSING_RECORD["case_id"]})
    monkeypatch.setattr(main, "get_case_by_id", lambda case_id: CLOSING_RECORD)
    payload_token = main._current_payload.set({"mode": "slack_app_mention", "text": "月次締めの自動化の件、店舗日報ダイジェストは別で本部の会議にも出してるんだよね"})
    intent_token = main._intent.set({"intent": "chat", "confidence": 0.6})
    try:
        result = json.loads(main.update_case_tool(additional_content="店舗日報ダイジェストは本部の会議にも出している"))
    finally:
        main._intent.reset(intent_token)
        main._current_payload.reset(payload_token)
    assert result["action"] == "register_or_update_question"
    assert fake.last_update_kwargs is None


def test_converse_calls_do_not_pass_temperature():
    # temperature の指定を受け付けないモデル（Claude Sonnet 5 など）があるため、converse に temperature を渡さない
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert '"temperature"' not in source


def test_pending_update_confirmation_is_blocked_when_intent_is_register(monkeypatch):
    # 確認待ちの残ったスレッドでも、登録依頼なら関門で止めて既存事例を更新しない
    fake = stub_persistence(monkeypatch)
    payload_token = main._current_payload.set({"mode": "slack_app_mention", "text": "「店舗日報ダイジェスト」の事例です。登録してください"})
    intent_token = main._intent.set({"intent": "register", "confidence": 0.95})
    try:
        result = json.loads(main.confirm_pending_update_tool(selection="1番"))
    finally:
        main._intent.reset(intent_token)
        main._current_payload.reset(payload_token)
    assert result["action"] == "register_or_update_question"
    assert fake.last_update_kwargs is None


def test_update_merges_card_fields_instead_of_replacing(monkeypatch):
    # 追記で取り組み・効果・課題を丸ごと置き換えず、前の版と統合する
    fake = stub_persistence(monkeypatch)
    record = {
        **sample_record(),
        "approach": "店舗ごとに検証用の発注データを用意し、ToolAで受け入れ試験",
        "impact": "人手の確認時間を45%削減",
        "technologies": ["ToolA"],
    }
    result = main.update_case_record(
        record,
        "ToolBで回帰テストも自動化。欠品の検知率が12%から31%へ向上",
        extracted_fields={"approach": "ToolBで回帰テストも自動化", "impact": "欠品の検知率が12%から31%へ向上", "technologies": ["ToolB"]},
    )
    assert result["ok"] is True
    values = fake.last_update_kwargs["ExpressionAttributeValues"]
    names = fake.last_update_kwargs["ExpressionAttributeNames"]
    saved = {names[k.replace(":attr", "#attr")]: v for k, v in values.items() if k.startswith(":attr")}
    assert "45%削減" in saved["impact"] and "31%" in saved["impact"]
    assert "ToolA" in saved["approach"] and "ToolB" in saved["approach"]
    assert set(saved["technologies"]) >= {"ToolA", "ToolB"}


def test_update_keeps_existing_title_unless_rename_requested(monkeypatch):
    # 追記では、依頼文中の別名にタイトルを置き換えない
    fake = stub_persistence(monkeypatch)
    main.update_case_record(sample_record(), "みどり書房さまの Shelf Forecast に効果を追記", extracted_fields={"title": "別のタイトル"})
    assert fake.last_update_kwargs["ExpressionAttributeValues"][":title"] == sample_record()["title"]
    fake2 = stub_persistence(monkeypatch)
    main.update_case_record(sample_record(), "タイトルを「新しい名前」に変えて", extracted_fields={"title": "新しい名前"})
    assert fake2.last_update_kwargs["ExpressionAttributeValues"][":title"] == "新しい名前"


# --- 意図判定の評価セット（eval/intent-cases.json）の形を確かめる。モデルは呼ばない ---

EVAL_BUSINESS_TOOLS = {
    "register_case_tool", "update_case_tool", "confirm_pending_update_tool", "ask_register_or_update_tool",
    "revert_case_tool", "search_cases_tool", "get_case_context_tool", "describe_case_tool",
    "case_next_action_tool", "get_case_graphic_tool", "describe_bot_tool", "chat_reply_tool",
    "unsupported_request_tool",
}


def test_eval_intent_cases_are_well_formed():
    data = json.loads((Path(__file__).resolve().parents[2] / "eval" / "intent-cases.json").read_text(encoding="utf-8"))
    seed_ids = [record["case_id"] for record in data["seed_cases"]]
    assert seed_ids and len(seed_ids) == len(set(seed_ids))
    assert all(main.CASE_ID_PATTERN.fullmatch(case_id) for case_id in seed_ids)
    case_ids = [case["id"] for case in data["cases"]]
    assert len(case_ids) == len(set(case_ids))
    for case in data["cases"]:
        assert case["text"].strip(), case["id"]
        assert case["expect"] and set(case["expect"]) <= EVAL_BUSINESS_TOOLS, case["id"]
        assert set(case.get("forbid", [])) <= EVAL_BUSINESS_TOOLS, case["id"]
        assert not set(case["expect"]) & set(case.get("forbid", [])), case["id"]
        state = case.get("state") or {}
        referenced = [state.get("case_id"), *state.get("candidate_case_ids", []), *state.get("last_search_case_ids", []), case.get("expect_case_id"), case.get("expect_proposed_case_id")]
        assert all(case_id in seed_ids for case_id in referenced if case_id), case["id"]

