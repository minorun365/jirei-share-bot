import base64
import concurrent.futures
import difflib
import io
import ipaddress
import json
import logging
import os
import re
import socket
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, urlencode, urlparse

import boto3
from boto3.dynamodb.conditions import Attr
from bedrock_agentcore import BedrockAgentCoreApp
from botocore.config import Config
from botocore.exceptions import ClientError
from google import genai
from strands import Agent, tool
from strands.models import BedrockModel
from strands_tools.http_request import http_request as strands_http_request


CASES_TABLE_NAME = os.environ["CASES_TABLE_NAME"]
THREAD_STATE_TABLE_NAME = os.environ["THREAD_STATE_TABLE_NAME"]
SHARE_LOG_TABLE_NAME = os.environ["SHARE_LOG_TABLE_NAME"]
CASE_ASSETS_BUCKET_NAME = os.environ["CASE_ASSETS_BUCKET_NAME"]
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "us-east-1"))
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID",
    "REPLACE_WITH_BEDROCK_INFERENCE_PROFILE_ID",
)
BEDROCK_MODEL_DISPLAY_NAME = os.environ.get("BEDROCK_MODEL_DISPLAY_NAME", "Amazon Bedrock model")
SLACK_WORKSPACE_URL = os.environ.get("SLACK_WORKSPACE_URL", "").rstrip("/")
GOOGLE_CLOUD_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
GOOGLE_CLOUD_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
GOOGLE_GENAI_IMAGE_MODEL_ID = os.environ.get(
    "GOOGLE_GENAI_IMAGE_MODEL_ID",
    os.environ.get("GEMINI_IMAGE_MODEL", ""),
)
GOOGLE_WIF_CREDENTIAL_CONFIG_PARAMETER_NAME = os.environ.get("GOOGLE_WIF_CREDENTIAL_CONFIG_PARAMETER_NAME", "")
GOOGLE_GENAI_USE_VERTEXAI = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() in {"1", "true", "yes", "on"}
KNOWLEDGE_BASE_ID = os.environ.get("CASE_KNOWLEDGE_BASE_ID", "")
KNOWLEDGE_BASE_DATA_SOURCE_ID = os.environ.get("CASE_KNOWLEDGE_BASE_DATA_SOURCE_ID", "")
CASE_KNOWLEDGE_SOURCE_PREFIX = os.environ.get("CASE_KNOWLEDGE_SOURCE_PREFIX", "knowledge-base/cases/")
CASE_KNOWLEDGE_RETRIEVAL_RESULTS = int(os.environ.get("CASE_KNOWLEDGE_RETRIEVAL_RESULTS", "5"))
GEMINI_API_KEY_PARAMETER_NAME = os.environ.get("GEMINI_API_KEY_PARAMETER_NAME", "/case-share-bot/gemini/api-key")
TAVILY_API_KEY_PARAMETER_NAME = os.environ.get("TAVILY_API_KEY_PARAMETER_NAME", "/case-share-bot/tavily/api-key")
GOOGLE_DRIVE_OAUTH_PARAMETER_NAME = os.environ.get(
    "GOOGLE_DRIVE_OAUTH_PARAMETER_NAME", "/case-share-bot/google/drive-oauth"
)
GOOGLE_DRIVE_HOSTS = {"docs.google.com", "drive.google.com"}
GOOGLE_DRIVE_ENABLED = os.environ.get("GOOGLE_DRIVE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL") or GOOGLE_GENAI_IMAGE_MODEL_ID
GEMINI_IMAGE_SIZE = os.environ.get("GEMINI_IMAGE_SIZE", "2K")
GRAPHIC_PROMPT_VERSION = "v2"
GRAPHIC_GENERATION_ENABLED = os.environ.get("GRAPHIC_GENERATION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
GRAPHIC_GENERATION_DELAY_SECONDS = int(os.environ.get("GRAPHIC_GENERATION_DELAY_SECONDS", "300"))
GRAPHIC_MAINTENANCE_BATCH_SIZE = int(os.environ.get("GRAPHIC_MAINTENANCE_BATCH_SIZE", "1"))
TAVILY_SEARCH_ENABLED = os.environ.get("TAVILY_SEARCH_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
TAVILY_SEARCH_MAX_RESULTS = int(os.environ.get("TAVILY_SEARCH_MAX_RESULTS", "3"))
IMAGE_ASSET_PREFIX = os.environ.get("IMAGE_ASSET_PREFIX", "images").strip("/") or "images"
GRAPHIC_PENDING_NOTICE = "追加情報があればメンションして教えてください。最終更新から5分後にグラレコ画像の生成が開始されます。"

MAX_SEARCH_SCAN_ITEMS = 300
THREAD_STATE_TTL_SECONDS = 90 * 24 * 60 * 60
PENDING_UPDATE_MAX_CANDIDATES = 3
# 更新対象の自動確定は「間違えたら既存事例が失われる」操作なので、曖昧なら必ず人間に確認する。
# 過去に定型句が混ざったヒントだけで別事例を選び、既存データを誤更新したため、
# 曖昧な候補は必ず利用者へ確認する。
AUTO_UPDATE_SCORE_GAP = 40
# 1位のスコアがこれ未満なら、候補が1件でも自動確定しない（弱い一致での誤爆を防ぐ）
AUTO_UPDATE_MIN_TOP_SCORE = 40
# 1位が2位のこの倍率以上でなければ自動確定しない（絶対差だけだと高スコア帯で競合を見落とす）
AUTO_UPDATE_SCORE_RATIO = 1.5
THREAD_TARGET_OVERRIDE_SCORE_GAP = 10
MAX_FOLLOW_UP_QUESTIONS = 2
MAX_SEARCH_RESULTS = 3
DAILY_SHARE_BODY_MAX_CHARS = int(os.environ.get("DAILY_SHARE_BODY_MAX_CHARS", "180"))
# 検索・詳細返信の紹介文は要点1〜2文に絞る。
# 表示時の強制カット上限ではなく、完結した文を選ぶための目安。
CASE_BODY_MAX_CHARS = int(os.environ.get("CASE_BODY_MAX_CHARS", "120"))
# summary 書き直しが失敗した際、巨大な取得本文をそのまま「追記」しないための安全上限。
SUMMARY_FALLBACK_UPDATE_MAX_CHARS = 180
MIN_SEARCH_SCORE = 20
MIN_RELATIVE_SEARCH_SCORE = 0.35
# Knowledge Base（S3 Vectors）の関連度スコアしきい値。ベクトル検索は無関係な質問にも
# 必ず上位N件を返すため、足切りが無いと「最もマシな無関係事例」を紹介してしまう。
# 2026-07-27 の本番実測: 無関係クエリ 0.78〜0.80 / 関連クエリ 0.83〜0.86 に分布。
KB_MIN_SEMANTIC_SCORE = float(os.environ.get("KB_MIN_SEMANTIC_SCORE", "0.80"))
KB_CONFIDENT_SEMANTIC_SCORE = float(os.environ.get("KB_CONFIDENT_SEMANTIC_SCORE", "0.83"))
# 足切り未満でも、このスコア以上なら「関連度は高くないがヒントになるかもしれない候補」として
# 最大2件だけ提示する。該当ゼロ回答ばかりになって有用性が下がるのを防ぐ緩衝帯。
KB_HINT_SEMANTIC_SCORE = float(os.environ.get("KB_HINT_SEMANTIC_SCORE", "0.75"))
MAX_HINT_RESULTS = 2
# 「はい、あります」と断定してよいレキシカル一致の下限。汎用語1個のかすり一致（20点）では断定しない。
STRONG_LEXICAL_SCORE = 40
MAX_FETCH_URLS = 3
MAX_FETCHED_SOURCE_CHARS = 5000
# Drive資料・添付ファイルは複数事例を含むことがあるため、Webページより上限を広げる
MAX_DOCUMENT_SOURCE_CHARS = 12000
MAX_TOTAL_FETCHED_SOURCE_CHARS = 24000
# Google Drive からダウンロードするバイナリ資料（PDF/PPTX等）の上限と形式
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
# Bedrock Converse の document ブロックは 1 ファイル 4.5MB まで
CONVERSE_DOCUMENT_MAX_BYTES = 4_500_000
CONVERSE_DOCUMENT_FORMATS = {"pdf", "csv", "doc", "docx", "xls", "xlsx", "html", "txt", "md"}
IMAGE_ATTACHMENT_FORMATS = {"png", "jpeg", "gif", "webp"}
URL_FETCH_TIMEOUT_SECONDS = 12
TAVILY_SEARCH_TIMEOUT_SECONDS = 12
TAVILY_SEARCH_ENDPOINT = "https://api.tavily.com/search"
TAVILY_EXTRACT_ENDPOINT = "https://api.tavily.com/extract"
TAVILY_EXTRACT_TIMEOUT_SECONDS = 25
EXTERNAL_URL_FETCH_ENABLED = os.environ.get("EXTERNAL_URL_FETCH_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
URL_FETCH_BROWSER_ENABLED = os.environ.get("URL_FETCH_BROWSER_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
BROWSER_FETCH_TIMEOUT_SECONDS = int(os.environ.get("BROWSER_FETCH_TIMEOUT_SECONDS", "150"))
BROWSER_TOOL_IDENTIFIER = os.environ.get("BROWSER_TOOL_IDENTIFIER", "aws.browser.v1")
MIN_READABLE_SOURCE_CHARS = 400
MAX_SLIDE_SCREENSHOTS = 8
SLIDE_HOSTS = {"speakerdeck.com", "www.slideshare.net", "slideshare.net", "www.docswell.com", "docswell.com"}
HELPER_TOOL_NAMES = {
    "http_request",
    "tavily_extract",
    "agentcore_browser",
    "tavily_search_tool",
    "retrieve_cases_tool",
    "get_case_context_tool",
}
# 返信文が完成している業務ツールの action。chat_reply_tool で上書きさせない。
TERMINAL_CASE_REPLY_ACTIONS = {"search", "describe", "handoff", "graphic"}
CASE_WRITE_ACTIONS = {"register", "update"}
# モデルが本文を書かずに穴埋めのまま残した返信文。Slackへ出さず直前の業務ツール結果へ戻す。
PLACEHOLDER_REPLY_TEXTS = {
    "placeholder",
    "placeholdertext",
    "yourtexthere",
    "tbd",
    "todo",
    "dummy",
    "dummytext",
    "sampletext",
    "n/a",
    "プレースホルダ",
    "プレースホルダー",
    "ダミー",
    "ダミーテキスト",
    "仮",
    "仮テキスト",
    "ここに本文",
    "本文",
    "未定",
    "省略",
}
MAX_PLACEHOLDER_REPLY_CHARS = 24
# OAuth済みDrive APIや実体確認済みPDFは、取得結果そのものが認証画面ではない。
# 資料本文に「ログイン」「パスワード」等が登場してもアクセス壁と誤判定しない。
TRUSTED_DOCUMENT_FETCH_METHODS = {"google_drive_api", "pdf_document"}

SPECIFIC_SEARCH_CATEGORY_TERMS = {
    "energy": ["エネルギー", "電力", "電力計", "energy"],
    "inquiry": ["FAQ", "ナレッジ", "問い合わせ", "問合せ", "問い合わせ削減", "問い合わせ対応"],
    "anomaly_detection": ["異常検知", "異常", "検知"],
    "review": ["レビュー", "提案資料", "LLM-as-a-Judge", "LLM-as-s-Jugde"],
    "multi_agent": ["マルチエージェント", "AgentCore", "Strands"],
}

TEST_MARKER_PATTERN = re.compile(r"\bE2E_TEST_[A-Za-z0-9_-]+\b")
CASE_ID_PATTERN = re.compile(r"\bcase-\d{14}-[0-9a-f]{8}\b")
URL_PATTERN = re.compile(r"https?://[^\s<>\]|()（）\"']+", re.IGNORECASE)
SEARCH_PATTERN = re.compile(r"(検索|探して|探す|教えて|ありますか|ある[？?]?|事例ある|事例は|事例を)")
DETAIL_PATTERN = re.compile(r"(詳細|詳しく|もっと|深掘り|内容は|その事例|それについて|紹介文|営業向け)")
CASE_PRODUCT_QUESTION_PATTERN = re.compile(
    r"(何を作|なにを作|どんな(?:プロダクト|製品|サービス|システム|アプリ)|"
    r"(?:プロダクト|製品|サービス|システム|アプリ)(?:の中身|の内容|なの|ですか|について))",
    re.IGNORECASE,
)
CASE_IMPACT_QUESTION_PATTERN = re.compile(
    r"(定量|効果|成果|実績|どれくらい|どのくらい|削減|向上|改善|工数|時間|何[%％])",
    re.IGNORECASE,
)
CASE_APPROACH_QUESTION_PATTERN = re.compile(
    r"(どうやって|どのように|進め方|開発方法|アプローチ|取り組み内容|何をした|なにをした)",
    re.IGNORECASE,
)
CASE_PROBLEM_QUESTION_PATTERN = re.compile(r"(課題|背景|きっかけ|なぜ|困って)", re.IGNORECASE)
CASE_TECHNOLOGY_QUESTION_PATTERN = re.compile(
    r"(技術|技術スタック|ツール|モデル|フレームワーク|アーキテクチャ)",
    re.IGNORECASE,
)
CASE_CONTACT_QUESTION_PATTERN = re.compile(r"(担当|誰に|だれに|聞き先|問い合わせ先)", re.IGNORECASE)
CASE_NEXT_ACTION_PATTERN = re.compile(
    r"(?:"
    r"(?:このあと|この後|ここから|今後|次(?:に|は)?).{0,24}(?:どう|何を|なにを|進め|動|すれ|すべき|アクション|一歩)"
    r"|(?:どう|どのように|何を|なにを).{0,18}(?:するのが|したら|すれば|進めれば|動けば).{0,8}(?:いい|よい|良い)"
    r"|(?:誰|だれ|どなた|どこ).{0,16}(?:聞|相談|連絡|つな|繋)"
    r"|(?:聞き先|相談先|連絡先|次のアクション|次の一歩|次の進め方)"
    r")",
    re.IGNORECASE,
)
CASE_PRODUCT_FACT_PATTERN = re.compile(
    r"(?:"
    r"(?:システム|サービス|プロダクト|アプリ|基盤|サイト|ツール|プラットフォーム).{0,40}(?:開発|構築|提供|運用)"
    r"|(?:開発|構築|提供|運用).{0,40}(?:システム|サービス|プロダクト|アプリ|基盤|サイト|ツール|プラットフォーム)"
    r")",
    re.IGNORECASE,
)
CASE_METRIC_FACT_PATTERN = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:[%％]|倍|時間|分|件|人日|ポイント)|"
    r"(?:半減|倍増|削減|短縮|向上|改善|増加|減少))",
    re.IGNORECASE,
)
CASE_DETAIL_LINK_REQUEST_PATTERN = re.compile(
    r"(?:"
    r"(?:事例|詳細|元資料)?(?:(?:へ)?の)?(?:URL|リンク)(?:先)?\s*(?:は|って|を)?\s*"
    r"(?:あります(?:か)?|ある[？?]?|教えて|ください|知りたい|どこ|取得したい)"
    r"|(?:事例|詳細|元資料)(?:(?:へ)?の)?(?:URL|リンク)(?:先)?\s*(?:は|って)?\s*[？?]?$"
    r"|(?:URL|リンク)(?:先)?\s*(?:は|って)\s*[？?]?$"
    r"|詳細ページ\s*(?:は|って|を)?\s*(?:あります(?:か)?|ある[？?]?|教えて|ください|どこ)"
    r"|詳細ページ\s*(?:は|って)?\s*[？?]?$"
    r"|詳しく見る(?:ための)?(?:URL|リンク|ページ)"
    r")",
    re.IGNORECASE,
)
SEARCH_EXISTENCE_QUESTION_PATTERN = re.compile(r"(?:あります(?:か|[？?])?|ある(?:か|[？?])|ない(?:です)?か)")
SEARCH_REFINEMENT_PATTERN = re.compile(r"(?:もっと|もう少し|ほかに|ほかの|他に|他の|別の|汎用的)")
HELP_REQUEST_PATTERN = re.compile(r"(使い方|ヘルプ|help|何ができる|なにができる|どう使う|使える機能|機能一覧)", re.IGNORECASE)
OUT_OF_SCOPE_REQUEST_PATTERN = re.compile(
    r"(天気|予定|カレンダー|メール送信|翻訳して|文章を書いて|コードを書いて|実装して|デバッグして|計算して|ニュース|株価|為替|雑談|自己紹介)",
    re.IGNORECASE,
)
BOT_SELF_REFERENCE_PATTERN = re.compile(
    r"(?:"
    r"この(?:AI)?(?:社内事例)?(?:Bot|bot|ボット|アプリ)"
    r"|AI社内事例おしえて君|社内事例おしえて君|おしえて君"
    r"|(?:君|あなた|Bot|bot|ボット)(?:自身|自体|の)"
    r")",
    re.IGNORECASE,
)
BOT_DESIGN_TOPIC_PATTERN = re.compile(
    r"(?:"
    r"設計|仕組み|構成|アーキテクチャ|技術スタック|実装|どう作|どう動|何で動"
    r"|グラレコ|画像生成|文字化け|フォント"
    r"|プロンプト|モデル|LLM|Claude|Gemini|Bedrock|AgentCore|Strands"
    r"|検索方式|RAG|ナレッジベース|データ保存|どこに保存"
    r"|セキュリティ|情報保護|個人情報|マスク|秘密情報"
    r"|日次共有|定期共有|スケジュール"
    r")",
    re.IGNORECASE,
)
# 「グラレコできた？」「グラレコある？」のように、事例のグラレコ画像そのものを求める質問。
# 「君のグラレコはどう作ってる？」のような設計質問は BOT_SELF_REFERENCE_PATTERN 側で
# 先に拾われるため、ここでは生成状況・受け取りを尋ねる語だけを対象にする。
CASE_GRAPHIC_REQUEST_PATTERN = re.compile(
    r"(?:グラレコ|グラフィックレコーディング|グラフィック・レコーディング)"
    r"[^\n]{0,16}?"
    r"(?:"
    r"でき(?:た|てる|ている|ました|上がった|あがった)|出来(?:た|てる|ている|上がった)"
    r"|完成|作れた|生成(?:できた|された|終わった|済)"
    r"|見せて|見たい|見れる|貼って|送って|ちょうだい|ください|どこ"
    r"|まだ[?？]|まだかな|どうなった|どうなってる|状況"
    r"|ある[?？]|あるの|ありますか|あります[?？]"
    r")",
    re.IGNORECASE,
)
BOT_CASE_TECHNOLOGY_QUESTION_PATTERN = re.compile(
    r"(?:"
    r"(?:この|その|さっきの|直前の|紹介した|見つけた|検索した|登録した|出した)?.{0,12}"
    r"(?:事例|案件|取り組み|取組|プロダクト).{0,30}"
    r"(?:モデル|技術|構成|アーキテクチャ|プロンプト)"
    r"|(?:モデル|技術|構成|アーキテクチャ|プロンプト).{0,30}"
    r"(?:事例|案件|取り組み|取組|プロダクト)"
    r")",
    re.IGNORECASE,
)
BOT_REANSWER_REQUEST_PATTERN = re.compile(
    r"(?:"
    r"改めて.{0,24}(?:答え|回答)"
    r"|(?:もう一度|もう1度|もっかい|再度).{0,24}(?:答え|回答)"
    r"|最初から.{0,24}(?:答え|回答)"
    r"|(?:答え|回答)(?:直して|直す|し直して|し直す)"
    r"|(?:質問|問い).{0,16}(?:答えて|回答して)"
    r")",
    re.IGNORECASE,
)
BOT_QUESTION_ADDRESSEE_PATTERN = re.compile(
    r"([A-Za-zぁ-んァ-ヶ一-龠々ー・]{1,24}さん)(?:の質問|からの質問|に.{0,12}(?:答え|回答))"
)
# このBotの収集対象（AIの活用・開発に関する社内事例）に関係しそうな質問かの判定。
# ASCII語は英単語の一部（email, detail 等）にマッチしないよう前後の英字を除外する。
AI_TOPIC_PATTERN = re.compile(
    r"(?:生成AI|機械学習|深層学習|ディープラーニング|チャットボット|プロンプト|"
    r"ファインチューニング|画像生成|音声認識|自然言語処理|ナレッジベース|エージェント|グラレコ|"
    r"(?<![A-Za-z])(?:AI|LLM|RAG|GPT|Claude|Gemini|Copilot|Bedrock|AgentCore|Strands|MCP)(?![A-Za-z]))",
    re.IGNORECASE,
)
AI_SCOPE_NOTE = (
    "このBotが集めているのはAIの活用・開発に関する社内事例なので、"
    "AIと直接関係しないテーマの事例はそもそも登録されていない可能性が高いです。"
)

EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"(?<![A-Za-z0-9_])(?:\+81[-\s]?)?0\d{1,4}[-\s]?\d{1,4}[-\s]?\d{3,4}(?![A-Za-z0-9_])")
TOKEN_PATTERN = re.compile(r"\b(?:xox[baprs]-[A-Za-z0-9-]+|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16})\b")
GOOGLE_API_KEY_PATTERN = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")
TAVILY_API_KEY_PATTERN = re.compile(r"\btvly-[A-Za-z0-9_-]{20,}\b", re.IGNORECASE)
PASSWORD_HINT_PATTERN = re.compile(r"(password|passwd|pwd|パスワード|api[_ -]?key|secret|token)\s*[:=]\s*\S+", re.IGNORECASE)
CUSTOMER_CONTACT_PATTERN = re.compile(r"(?:顧客担当者|お客様担当|先方担当|ご担当者)[^\n。]*(?:さん|様|氏)")
BLOCKED_FETCH_HOSTS = {"localhost", "metadata.google.internal", "169.254.169.254"}
MEMBERS_WALL_PATTERN = re.compile(
    r"(会員限定|無料登録すると|有料会員|メンバー限定|続きを読むには|members[- ]only|subscribers[- ]only|log ?in to (?:read|view|continue))",
    re.IGNORECASE,
)

_current_payload: ContextVar[dict[str, Any]] = ContextVar("current_payload", default={})
_tool_calls: ContextVar[list[str]] = ContextVar("tool_calls", default=[])
# Strands の同期ツールはコピーされた別コンテキストで実行されることがある。
# ContextVar.set() の再代入は親へ戻らないため、ツール結果は共有リストへの追記で伝える。
_tool_results: ContextVar[list[dict[str, Any]]] = ContextVar("tool_results", default=[])
_web_source_urls: ContextVar[list[str]] = ContextVar("web_source_urls", default=[])
_fetched_url_sources: ContextVar[list[dict[str, Any]]] = ContextVar("fetched_url_sources", default=[])
_retrieved_case_ids: ContextVar[list[str]] = ContextVar("retrieved_case_ids", default=[])
_retrieval_results: ContextVar[list[dict[str, Any]]] = ContextVar("retrieval_results", default=[])
# ツールは別コンテキストで実行され得るため、.set() でなくリストへの追記（mutation）で親へ伝える
_graphic_pending_scheduled: ContextVar[list[bool]] = ContextVar("graphic_pending_scheduled", default=[])

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
ssm = boto3.client("ssm")
bedrock_retry_config = Config(retries={"max_attempts": 5, "mode": "adaptive"})
bedrock_agent_runtime = boto3.client("bedrock-agent-runtime", region_name=BEDROCK_REGION, config=bedrock_retry_config)
bedrock_agent = boto3.client("bedrock-agent", region_name=BEDROCK_REGION, config=bedrock_retry_config)
bedrock_runtime = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION, config=bedrock_retry_config)

cases_table = dynamodb.Table(CASES_TABLE_NAME)
thread_state_table = dynamodb.Table(THREAD_STATE_TABLE_NAME)
share_log_table = dynamodb.Table(SHARE_LOG_TABLE_NAME)

app = BedrockAgentCoreApp()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


SYSTEM_PROMPT = """あなたは社内の取り組み事例を共有するSlack Bot「事例共有くん」の中核エージェントです。
Slackの自然文を読み、必要なら補助ツールで公開情報を確認し、最後は必ず1つだけ業務ツールを呼び出してください。ツール結果をもとに、Slackに返すJSONだけを返します。

重要な運用ルール:
- Slackに投稿できるのは社内共有向けの通常業務情報です。
- メールアドレス、電話番号、顧客担当者名、APIキー、パスワード、トークンは投稿・保存しません。ツール側でマスクまたはブロックします。
- 顧客名、案件名、社内部署名、社内担当者の名字や表示名、取り組み概要は、通常の社内Slack共有として扱えます。
- 社内担当者の人名をSlackに表示するときは、呼び捨てにせず、一人ずつ「さん」を付けてください。複数名を列挙する場合も全員に付けてください。
- 組織が外部共有を認めていない高機密情報、顧客個人情報、従業員センシティブ情報、特定者限定ログやDB情報は扱いません。

ツール選択:
- 会社名、製品名、公開登壇資料名、具体的な技術キーワードから公開Web情報を補うと登録・更新の質が上がる場合だけ、最終業務ツールの前に tavily_search_tool を呼び出してよいです。
- tavily_search_tool は公開Web検索専用です。社内情報、顧客機微情報、個人情報、Slack本文の秘密情報を検索クエリに入れないでください。
- 探して、教えて、検索、ありますか、事例ある、または短い質問では、まず retrieve_cases_tool でKnowledge Baseを検索し、その結果を踏まえて最後に search_cases_tool を呼び出してください。
- mode が daily_share の場合は pick_daily_share_tool。
- mode が graphic_maintenance の場合はツールを呼びません。ランタイム側が保留中の画像生成を処理します。
- mode が knowledge_base_sync の場合はツールを呼びません。ランタイム側がS3 Markdown再生成とKnowledge Base同期を処理します。
- テスト削除と E2E_TEST_... がある場合は delete_test_cases_tool。
- thread_state.pending_action が update_case の場合は confirm_pending_update_tool を最優先で呼び出します。
- thread_state.last_search_case_ids があり、「その詳細は？」「詳しく」などの追質問なら、先に get_case_context_tool で登録情報を読み、その結果だけを根拠に describe_case_tool を呼び出してください。
- thread_state.last_search_case_ids または thread_state.case_id がある会話で、「このあとどうする？」「誰に聞けばいい？」「次のアクションは？」のように発見後の進め方を聞かれたら case_next_action_tool を呼び出してください。これは対象外質問ではなく、このBotの主要な役割である担当者との橋渡しです。
- describe_case_tool の answer には質問へ直接答える自然な文章を渡します。登録情報にないことは「登録情報では分からない」と明記し、概要の繰り返しや推測で埋めないでください。担当者への案内はツール側で付けるため answer には書きません。
- 「グラレコできた？」「グラレコある？」「グラレコ見せて」のように事例のグラレコ画像そのものを求められたら get_case_graphic_tool を呼び出してください。生成状況はツールが保存済みの値で確認します。自分で推測して「出来ているはずです」と答えたり、チャンネルを見に行くよう利用者へ頼んだりしてはいけません。
- search_cases_tool、describe_case_tool、case_next_action_tool、get_case_graphic_tool のいずれかを呼び出した後は、chat_reply_tool など別の業務ツールを追加で呼ばず、そのツール結果を最終応答としてそのまま返してください。
- 直前の検索に対する「もっと汎用的なもの」「ほかの事例」「別の案件」は詳細質問ではなく検索条件の変更です。retrieve_cases_tool の後に search_cases_tool を呼びます。
- 事例の詳細URL・詳細リンクを聞かれた場合も describe_case_tool を呼び、question にユーザーの質問を省略せず渡します。リンクを推測や創作で返してはいけません。
- 追記、更新、補足、追加情報、ブラッシュアップは update_case_tool。
- 登録、追加、覚えて、保存、または十分長い取り組み説明は register_case_tool。
- 探して、教えて、検索、ありますか、事例ある、または短い質問は search_cases_tool。
- 「URL読めてないの？」「なんでこの内容？」「さっき何した？」のようなBotの動作・直前の処理・URL読み取り状況への質問、お礼、軽い確認には chat_reply_tool で自然な文章で答えてください。fetched_url_sources や thread_state.last_url_fetch_results の事実（どの手段で読めたか、なぜ読めなかったか）に基づいて説明し、分からないことは分からないと言ってください。定型の使い方案内で済ませないでください。
- 「このBotのモデルは？」「君のグラレコはどう作ってる？」「このアプリの設計は？」のような、このBot自身の構成・実装・画像生成・検索・情報保護・定期処理への質問には describe_bot_tool。社内事例の技術やモデルを尋ねる質問とは区別してください。
- 事例共有と全く関係ない作業依頼（天気、予定、翻訳、文章作成、コード作成など）だけ unsupported_request_tool。

登録・更新で重要なこと:
- ユーザーの命令文をそのままタイトルや概要にしないでください。
- 更新時の summary には「〜が正しい」「〜を修正します」「〜を追記」のような修正依頼の言い換えを渡さないでください。既存の紹介文に修正・追記を反映した完成形の紹介文だけを渡し、既存の紹介文が分からなければ summary は空にしてください（ランタイム側で既存紹介文に反映します）。
- 法人顧客名、案件名、部署、社内担当者名、技術、課題、取り組み、効果を読み取り、社内共有用の自然な事例カードに整理してください。
- summary は「何をした事例か」が伝わる1〜2つの完結文にし、全体をおおむね120文字以内に収めてください。文の途中で切らず、末尾に「…」を付けた省略文も作らないでください。
- Tavily検索結果は、顧客の公開事業領域、製品カテゴリ、公開されている技術背景など、確実な範囲の補足にだけ使ってください。検索結果の本文を長くコピーせず、業界・用途・タグ・概要をうっすら補う程度に要約してください。
- Tavily検索を使った場合は、参照した公開URLを register_case_tool / update_case_tool の external_source_urls に渡して内部 traceability には残してよいですが、Slack表示文には参考URLを書かないでください。
- 分からない項目は推測しすぎず空欄にし、必要な追加質問を最大2つに絞ってください。
- 業界や用途が推定できる場合は customer_industry や tags に入れてください。例: 電力会社、電力計 -> エネルギー / 電力。

応答形式:
- 最終応答は必ずJSONオブジェクトのみ。
- ツールが返したJSONをそのまま返してください。説明文やMarkdownフェンスは付けません。
- 「○○の事例紹介！」や :tada: のような告知調は daily_share 専用です。Slackで質問された時は、問いに直接答える短い会話文にしてください。
"""


def create_agent() -> Agent:
    # 複数事例の登録では tool use の引数が長くなる。既定の max_tokens だと
    # 応答が途中で切れて MaxTokensReachedException になるため明示的に広げる
    model = BedrockModel(region_name=BEDROCK_REGION, model_id=BEDROCK_MODEL_ID, max_tokens=16384)
    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[
            tavily_search_tool,
            retrieve_cases_tool,
            register_case_tool,
            update_case_tool,
            confirm_pending_update_tool,
            search_cases_tool,
            get_case_context_tool,
            describe_case_tool,
            case_next_action_tool,
            get_case_graphic_tool,
            describe_bot_tool,
            chat_reply_tool,
            unsupported_request_tool,
            delete_test_cases_tool,
            pick_daily_share_tool,
        ],
    )


_agent: Agent | None = None
_gemini_client: genai.Client | None = None
_gemini_api_key: str | None = None
_google_credentials_path: str | None = None
_tavily_api_key: str | None = None


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = create_agent()
    return _agent


@tool
def tavily_search_tool(query: str, purpose: str = "", max_results: int = 3) -> str:
    """登録・更新の補足に使う公開Web検索をTavilyで実行します。

    Args:
        query: 公開Webで検索する法人名、製品名、公開技術キーワード。秘密情報や個人情報は含めません。
        purpose: 検索したい観点。例: 顧客の業界確認、製品カテゴリ確認、技術背景の補足。
        max_results: 取得する検索結果数。最大3件。

    Returns:
        検索結果のJSON文字列。登録・更新ツールに渡すための公開情報スニペットとURLを含みます。
    """
    record_tool_call("tavily_search_tool")
    if not TAVILY_SEARCH_ENABLED:
        return json.dumps({"ok": False, "error": "tavily_search_disabled", "results": []}, ensure_ascii=False)

    redaction = sanitize_slack_text(" ".join(part for part in [query, purpose] if part))
    if redaction["status"] == "blocked":
        return json.dumps({"ok": False, "error": "query_contains_blocked_information", "results": []}, ensure_ascii=False)

    search_query = cleanup_generated_text(query)
    if not search_query or len(search_query) < 2:
        return json.dumps({"ok": False, "error": "empty_query", "results": []}, ensure_ascii=False)

    result_limit = clamp_int(max_results, default=3, minimum=1, maximum=min(TAVILY_SEARCH_MAX_RESULTS, 3))
    request_body = {
        "query": search_query,
        "search_depth": "basic",
        "topic": "general",
        "max_results": result_limit,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
        "include_favicon": False,
        "country": "japan",
    }
    try:
        api_key = get_tavily_api_key()
    except Exception as exc:
        log_event("tavily_api_key_unavailable", error_type=type(exc).__name__)
        return json.dumps({"ok": False, "error": "tavily_api_key_unavailable", "results": []}, ensure_ascii=False)

    request = urlrequest.Request(
        TAVILY_SEARCH_ENDPOINT,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AI-Shanai-Jirei-Oshiete-Kun/0.1",
        },
        method="POST",
    )

    try:
        with urlrequest.urlopen(request, timeout=TAVILY_SEARCH_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        log_event("tavily_search_http_error", status_code=exc.code)
        return json.dumps({"ok": False, "error": "tavily_http_error", "status_code": exc.code, "results": []}, ensure_ascii=False)
    except Exception as exc:
        log_event("tavily_search_failed", error_type=type(exc).__name__)
        return json.dumps({"ok": False, "error": "tavily_request_failed", "results": []}, ensure_ascii=False)

    results = normalize_tavily_results(payload)
    source_urls = [result["url"] for result in results if result.get("url")]
    if source_urls:
        _web_source_urls.set(merge_external_source_urls(_web_source_urls.get(), source_urls))
    log_event("tavily_search_completed", result_count=len(results))
    return json.dumps(
        {
            "ok": True,
            "query": search_query,
            "purpose": cleanup_generated_text(purpose),
            "results": results,
        },
        ensure_ascii=False,
    )


@tool
def retrieve_cases_tool(query: str, number_of_results: int = 5) -> str:
    """Bedrock Knowledge Baseから登録済み社内事例Markdownをセマンティック検索します。

    検索質問では、最後のSlack返信を作る search_cases_tool の前にこのツールを使ってください。
    このツールは候補となるcase_idと短い抜粋だけを返します。Slack表示文は返しません。
    メールアドレス、電話番号、秘密情報、顧客担当者名を含む検索語はマスク・ブロック対象です。

    Args:
        query: 探したいテーマ、顧客名、業界、技術、課題など。1000文字以内に丸めます。
        number_of_results: 取得する候補数。最大8件。

    Returns:
        Knowledge Baseの検索結果JSON文字列。case_id、score、excerptを含みます。
    """
    retrieval = retrieve_cases_from_knowledge_base(query, number_of_results, record_call=True)
    return json.dumps(retrieval, ensure_ascii=False)


@tool
def register_case_tool(
    content: str,
    title: str = "",
    summary: str = "",
    project_or_customer: str = "",
    customer_industry: str = "",
    department: str = "",
    contact_hint: str = "",
    problem: str = "",
    approach: str = "",
    impact: str = "",
    technologies: list[str] | None = None,
    tags: list[str] | None = None,
    follow_up_questions: list[str] | None = None,
    external_source_urls: list[str] | None = None,
    allow_duplicate: bool = False,
) -> str:
    """Slack投稿から社内事例を登録します。

    Args:
        content: 登録したい事例の原文。Slackメンションは除去済みでなくてもよいです。
        title: 社内共有用に整えた事例タイトル。命令文や「登録して」は含めません。
        summary: 社内共有用の概要。何をした事例かが分かる1〜2つの完結文にし、全体をおおむね120文字以内に収めます。途中切断や末尾の「…」は使いません。
        project_or_customer: 顧客名、案件名、社内プロジェクト名など。
        customer_industry: 業界や事業領域。例: エネルギー、通信、小売、公共。
        department: 担当部署やチーム。
        contact_hint: Slack投稿に自然に出してよい社内担当者名、名字、部署名。
        problem: 解決した課題。
        approach: AI、システム、運用として何をしたか。
        impact: 効果、反応、短縮時間、品質向上など。分からなければ空欄。
        technologies: 利用技術やキーワード。
        tags: 検索しやすくするためのタグ。業界、用途、技術、顧客名の別名を含めます。
        follow_up_questions: 追加で聞くと事例が良くなる質問。最大2つ。
        external_source_urls: Tavily検索やURL取得で参照した公開情報のURL。最大10件まで保存します。
        allow_duplicate: 既存事例と似ていても新規登録する場合だけ true。ユーザーが「別件です」と明言した時に限り使います。

    Returns:
        Slack返信用のJSON文字列。case_id、タイトル、概要、マスク状況を含みます。
        既存事例と重複の疑いがある場合は action=duplicate_suspected と候補一覧を返し、登録しません。
    """
    payload = _current_payload.get()
    redaction = sanitize_slack_text(content)
    if redaction["status"] == "blocked":
        return remember_tool_result(
            {
                "ok": False,
                "action": "register",
                "slack_text": "API key / token / password らしき情報を検出したため、登録しませんでした。該当部分を削除して再投稿してください。",
            },
            "register_case_tool",
        )

    now = utc_now_iso()
    # LLMがcontentからマーカーを落とすことがあるため、元のSlack本文からも拾う
    test_marker = extract_test_marker(content) or extract_test_marker(str(payload.get("text", "")))
    structured = normalize_case_card_fields(
        redaction["text"],
        {
            "title": title,
            "summary": summary,
            "project_or_customer": project_or_customer,
            "customer_industry": customer_industry,
            "department": department,
            "contact_hint": contact_hint,
            "problem": problem,
            "approach": approach,
            "impact": impact,
            "technologies": technologies or [],
            "tags": tags or [],
        },
    )
    if structured["blocked"]:
        return remember_tool_result(
            {
                "ok": False,
                "action": "register",
                "slack_text": "API key / token / password らしき情報を検出したため、登録しませんでした。該当部分を削除して再投稿してください。",
            },
            "register_case_tool",
        )

    attributes = {
        **extract_case_attributes(redaction["text"]),
        **{key: value for key, value in structured.items() if key not in {"blocked", "sensitive_flags"} and value},
    }
    external_source_urls = merge_external_source_urls(
        collect_external_source_urls(redaction["text"]),
        [*current_payload_source_urls(), *current_web_source_urls(), *normalize_external_source_url_list(external_source_urls or [])],
    )
    title_value = str(attributes.pop("title", "") or build_case_title(redaction["text"]))
    summary_value = str(attributes.pop("summary", "") or build_summary(redaction["text"]))
    sensitive_flags = sorted(set(redaction["flags"] + structured.get("sensitive_flags", [])))
    missing_questions = build_follow_up_questions(
        {
            "title": title_value,
            "summary": summary_value,
            **attributes,
        },
        follow_up_questions or [],
    )
    status = determine_case_status(title_value, summary_value, attributes)
    record = {
        "case_id": build_case_id(now),
        "title": title_value,
        "summary": summary_value,
        "status": status,
        "visibility": "internal_share_ok",
        "source_channel_id": payload_channel_id(payload),
        "source_thread_ts": payload.get("thread_ts", payload.get("message_ts", "")),
        "source_message_ts": payload.get("message_ts", ""),
        "source_message_url": build_slack_message_url(payload_channel_id(payload), payload.get("message_ts", "")),
        "keywords": build_keywords(redaction["text"], attributes),
        "created_at": now,
        "updated_at": now,
        "redaction_status": "masked" if sensitive_flags else redaction["status"],
        "sensitive_flags": sensitive_flags,
        **attributes,
    }
    if external_source_urls:
        record["external_source_urls"] = external_source_urls
    if payload.get("user_id"):
        record["owner_user_id"] = payload["user_id"]
    if test_marker:
        record["test_marker"] = test_marker

    # KBの同期ラグ（15分毎）があるため、登録直前にDynamoDBを直接照合して重複を防ぐ
    if not allow_duplicate:
        duplicates = find_duplicate_case_candidates(record["title"], str(record.get("project_or_customer") or ""))
        if duplicates:
            log_event("register_duplicate_suspected", title_chars=len(record["title"]), count=len(duplicates))
            candidate_lines = "、".join(
                f"「{candidate.get('title', '')}」({candidate['case_id']})" for candidate in duplicates[:3]
            )
            return remember_tool_result(
                {
                    "ok": False,
                    "action": "duplicate_suspected",
                    "candidates": [
                        {
                            "case_id": candidate["case_id"],
                            "title": candidate.get("title", ""),
                            "updated_at": candidate.get("updated_at", ""),
                        }
                        for candidate in duplicates[:3]
                    ],
                    "slack_text": (
                        f"「{record['title']}」は既存事例 {candidate_lines} と同じ取り組みの可能性があるため、登録を保留しました。"
                        "同じ事例でしたら追記として反映します。別件でしたら「別件として登録して」と返信してください。"
                    ),
                },
                "register_case_tool",
            )

    cases_table.put_item(Item=record, ConditionExpression="attribute_not_exists(case_id)")
    put_case_markdown(record)
    save_thread_state(record)
    record = schedule_case_graphic(record)

    # 永続化はここまでで完了。以降の返信整形で例外が出てもツールエラーにせず、
    # 登録済みであることが伝わる簡易メッセージで返す（2026-07-09 障害の再発防止）
    try:
        summary_lines = [format_case_paragraph(record)]
        display_tags = build_display_tags(record)
        if display_tags:
            summary_lines.append(f"検索用には {', '.join(display_tags)} あたりのキーワードで探せるようにしておきます。")
        summary_lines.extend(format_source_url_bullets(record))
        notice_lines: list[str] = []
        notice_lines.extend(format_url_fetch_notice_lines())
        if record["redaction_status"] == "masked":
            notice_lines.append("※ メールアドレス・電話番号・顧客担当者名などは伏せ字化して保存しました。")
        if record.get("graphic_status") == "pending":
            notice_lines.append(GRAPHIC_PENDING_NOTICE)
            _graphic_pending_scheduled.get().append(True)
        follow_up_lines: list[str] = []
        if missing_questions:
            follow_up_lines.append(
                "追加情報があれば、このスレッドで追加で Bot にメンションして教えてください。"
                f" 特に {format_inline_questions(missing_questions)} が分かると、紹介文をもう少し良くできます。"
            )

        result = with_case_graphic(
            {
                "ok": True,
                "action": "register",
                "case_id": record["case_id"],
                "slack_text": format_slack_sections(
                    f"事例として登録しました。「{cleanup_generated_text(str(record.get('title') or '社内AI活用'))}」として、社内共有用にはこう整理しています。",
                    summary_lines,
                    notice_lines,
                    follow_up_lines,
                ),
            },
            record,
        )
    except Exception as exc:
        log_event("register_reply_format_failed", case_id=record["case_id"], error_type=type(exc).__name__)
        result = {
            "ok": True,
            "action": "register",
            "case_id": record["case_id"],
            "slack_text": f"事例として登録し、保存まで完了しています: {record['title']}",
        }

    return remember_tool_result(result, "register_case_tool")


@tool
def update_case_tool(
    additional_content: str,
    title: str = "",
    summary: str = "",
    project_or_customer: str = "",
    customer_industry: str = "",
    department: str = "",
    contact_hint: str = "",
    problem: str = "",
    approach: str = "",
    impact: str = "",
    technologies: list[str] | None = None,
    tags: list[str] | None = None,
    external_source_urls: list[str] | None = None,
) -> str:
    """Slackの自然文から既存事例を更新します。

    同じSlackスレッドに事例が紐づいていればその事例を更新します。
    紐づきがない場合は、本文中の顧客名・案件名・case_idなどをヒントに候補を検索し、
    1件に絞れる場合は自動更新、曖昧な場合は候補を提示して確認待ちにします。

    Args:
        additional_content: 追記したい内容。
        title: 更新後により自然なタイトルへ直す場合のタイトル。
        summary: 修正・追記を反映した「更新後の完成形の紹介文」。1〜2つの完結文にし、全体をおおむね120文字以内に収めます。途中切断や末尾の「…」は使いません。
            「〜が正しい」「〜を追記します」のような修正依頼の言い換えは渡さないでください。
            既存の紹介文が会話から分からない場合は空文字にしてください（ランタイム側で既存紹介文に反映します）。
        project_or_customer: 顧客名、案件名、社内プロジェクト名など。
        customer_industry: 業界や事業領域。
        department: 担当部署やチーム。
        contact_hint: Slack投稿に自然に出してよい社内担当者名、名字、部署名。
        problem: 解決した課題。
        approach: AI、システム、運用として何をしたか。
        impact: 効果、反応、短縮時間、品質向上など。
        technologies: 利用技術やキーワード。
        tags: 検索しやすくするためのタグ。
        external_source_urls: Tavily検索やURL取得で参照した公開情報のURL。最大10件まで保存します。

    Returns:
        Slack返信用のJSON文字列。更新された概要を含みます。
    """
    payload = _current_payload.get()
    content = tool_content_with_payload(additional_content)
    structured_fields = {
        "title": title,
        "summary": summary,
        "project_or_customer": project_or_customer,
        "customer_industry": customer_industry,
        "department": department,
        "contact_hint": contact_hint,
        "problem": problem,
        "approach": approach,
        "impact": impact,
        "technologies": technologies or [],
        "tags": tags or [],
        "external_source_urls": external_source_urls or [],
    }
    state = get_current_thread_state(payload)
    if state and state.get("pending_action") == "update_case":
        return remember_tool_result(confirm_pending_update(content, "", structured_fields), "update_case_tool")

    if state and state.get("case_id"):
        record = get_case_by_id(str(state["case_id"]))
        if not record:
            return remember_tool_result(
                {
                    "ok": False,
                    "action": "update",
                    "slack_text": "更新対象の事例が見つかりませんでした。",
                },
                "update_case_tool",
            )
        if should_resolve_explicit_update_target(content, record):
            return remember_tool_result(resolve_update_target_and_update(content, structured_fields), "update_case_tool")
        return remember_tool_result(update_case_record(record, content, extracted_fields=structured_fields), "update_case_tool")

    return remember_tool_result(resolve_update_target_and_update(content, structured_fields), "update_case_tool")


@tool
def confirm_pending_update_tool(selection: str, additional_content: str = "") -> str:
    """更新候補の確認待ちスレッドで、選択内容をもとに事例を確定して更新します。

    Args:
        selection: 「1番」「この事例」「製造業A社」など、候補を特定する返答。
        additional_content: 選択と同時に追記したい追加情報。なければ空文字。

    Returns:
        Slack返信用のJSON文字列。更新結果または再確認の候補を含みます。
    """
    return remember_tool_result(confirm_pending_update(tool_content_with_payload(selection), additional_content, None), "confirm_pending_update_tool")


def resolve_update_target_and_update(additional_content: str, extracted_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    redaction = sanitize_slack_text(additional_content)
    if redaction["status"] == "blocked":
        return {
            "ok": False,
            "action": "update",
            "slack_text": "API key / token / password らしき情報を検出したため、追記しませんでした。該当部分を削除して再投稿してください。",
        }

    case_id = extract_case_id(redaction["text"])
    if case_id:
        record = get_case_by_id(case_id)
        if record:
            return update_case_record(record, redaction["text"], extracted_fields=extracted_fields)

    # 利用者が正式タイトルを引用して指名している場合は、スコアリングより指名を優先する
    quoted_matches = find_cases_by_quoted_title(redaction["text"])
    if len(quoted_matches) == 1:
        record = quoted_matches[0]
        log_event("update_target_resolved_by_quoted_title", case_id=record["case_id"])
        return update_case_record(
            record,
            redaction["text"],
            response_prefix=f"指名された「{record['title']}」を更新しました。",
            extracted_fields=extracted_fields,
        )

    target_hint = extract_update_target_hint(redaction["text"])
    candidates = rank_cases(target_hint or redaction["text"])
    if len(quoted_matches) > 1:
        # 同名タイトルが複数あるときは確定させず、引用された候補だけを提示して選んでもらう
        save_pending_update(redaction["text"], target_hint, quoted_matches[:PENDING_UPDATE_MAX_CANDIDATES])
        return {
            "ok": True,
            "action": "update_pending",
            "slack_text": format_slack_sections(
                "更新したい内容は預かりました。同じタイトルの事例が複数あるため、このスレッドで Bot にメンションして「1番」のように選んでください。",
                format_candidate_lines(quoted_matches[:PENDING_UPDATE_MAX_CANDIDATES]),
            ),
        }

    if should_auto_update(candidates):
        record = candidates[0]["record"]
        return update_case_record(
            record,
            redaction["text"],
            response_prefix=f"「{target_hint or record['title']}」から更新対象を「{record['title']}」と判断して更新しました。",
            extracted_fields=extracted_fields,
        )

    log_event(
        "update_target_ambiguous",
        target_hint=target_hint[:120],
        candidate_case_ids=[candidate["record"]["case_id"] for candidate in candidates[:PENDING_UPDATE_MAX_CANDIDATES]],
        top_score=candidates[0]["score"] if candidates else 0,
        second_score=candidates[1]["score"] if len(candidates) > 1 else 0,
    )
    save_pending_update(redaction["text"], target_hint, [candidate["record"] for candidate in candidates[:PENDING_UPDATE_MAX_CANDIDATES]])
    if not candidates:
        return {
            "ok": False,
            "action": "update_pending",
            "slack_text": "更新したい内容は預かりましたが、対象の事例を特定できませんでした。このスレッドで Bot にメンションして「製造業A社の案件です」のように、顧客名・案件名をもう少し教えてください。",
        }

    return {
        "ok": True,
        "action": "update_pending",
        "slack_text": format_slack_sections(
            "更新したい内容は預かりました。どの事例を更新すればよいか確信が持てなかったので、このスレッドで Bot にメンションして「1番」のように選んでください。",
            format_candidate_lines([candidate["record"] for candidate in candidates[:PENDING_UPDATE_MAX_CANDIDATES]]),
        ),
    }


def should_resolve_explicit_update_target(additional_content: str, thread_record: dict[str, Any]) -> bool:
    text = normalize_slack_text(additional_content)
    explicit_case_id = extract_case_id(text)
    if explicit_case_id:
        return explicit_case_id != thread_record.get("case_id")

    # 正式タイトルを引用して別事例を指名している場合は、スレッド紐付けより指名を優先する
    quoted_matches = find_cases_by_quoted_title(text)
    if quoted_matches:
        return any(record.get("case_id") != thread_record.get("case_id") for record in quoted_matches)

    if not has_explicit_update_target_signal(text):
        return False

    target_hint = extract_update_target_hint(text)
    if not target_hint or is_thread_reference_target_hint(target_hint):
        return False

    candidates = rank_cases(target_hint)
    if not candidates:
        return True

    top_record = candidates[0]["record"]
    if top_record.get("case_id") == thread_record.get("case_id"):
        return False

    thread_score = score_case(thread_record, target_hint)
    return candidates[0]["score"] - thread_score >= THREAD_TARGET_OVERRIDE_SCORE_GAP


def has_explicit_update_target_signal(text: str) -> bool:
    normalized = normalize_slack_text(text)
    if re.search(r"(?:別件|別の案件|他の案件|違う案件|異なる案件|ではなく|じゃなく|じゃなくて|のほう|の方)", normalized):
        return True
    if re.search(r"(?:こちら|この|その|本件|今回)(?:の)?(?:案件|事例|内容|情報)", normalized):
        return False
    return bool(re.search(r"[\w一-龯ぁ-んァ-ヶー]{2,}(?:の)?(?:案件|事例|内容|情報)(?:を|も|について|に関して|更新|追記)", normalized))


def is_thread_reference_target_hint(target_hint: str) -> bool:
    normalized = normalize_slack_text(target_hint)
    return bool(re.fullmatch(r"(?:こちら|この|その|本件|今回)(?:の)?(?:案件|事例|内容|情報)?", normalized))


def confirm_pending_update(selection: str, additional_content: str, extracted_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _current_payload.get()
    state = get_current_thread_state(payload)
    if not state or state.get("pending_action") != "update_case":
        return resolve_update_target_and_update(" ".join(part for part in [selection, additional_content] if part), extracted_fields)

    pending_content = str(state.get("additional_content", ""))
    candidate_records = get_cases_by_ids([str(case_id) for case_id in state.get("candidate_case_ids", [])])
    selected = select_candidate(selection, candidate_records)
    if not selected:
        combined_hint = " ".join(part for part in [state.get("target_hint", ""), selection] if part)
        ranked = rank_cases(combined_hint)
        candidate_records = [candidate["record"] for candidate in ranked[:PENDING_UPDATE_MAX_CANDIDATES]]
        selected = candidate_records[0] if should_auto_update(ranked) else None

    if not selected:
        save_pending_update(pending_content, str(state.get("target_hint", "")), candidate_records)
        return {
            "ok": True,
            "action": "update_pending",
            "slack_text": format_slack_sections(
                "まだ更新対象を1件に絞れませんでした。対象の番号、またはもう少し具体的な案件名で教えてください。",
                format_candidate_lines(candidate_records),
            ),
        }

    extra_update = additional_content if looks_like_update_content(additional_content) else ""
    update_content = "\n".join(part for part in [pending_content, extra_update] if part)
    return update_case_record(
        selected,
        update_content,
        response_prefix=f"確認できたので「{selected['title']}」を更新しました。",
        extracted_fields=extracted_fields,
    )


def update_case_record(record: dict[str, Any], additional_content: str, response_prefix: str | None = None, extracted_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    redaction = sanitize_slack_text(additional_content)
    if redaction["status"] == "blocked":
        return {
            "ok": False,
            "action": "update",
            "slack_text": "API key / token / password らしき情報を検出したため、追記しませんでした。該当部分を削除して再投稿してください。",
        }

    now = utc_now_iso()
    raw_extracted_fields = extracted_fields or {}
    explicit_source_urls = normalize_external_source_url_list(raw_extracted_fields.get("external_source_urls", []))
    structured = normalize_case_card_fields(
        redaction["text"],
        {key: value for key, value in raw_extracted_fields.items() if key != "external_source_urls"},
        fill_defaults=False,
    )
    if structured["blocked"]:
        return {
            "ok": False,
            "action": "update",
            "slack_text": "API key / token / password らしき情報を検出したため、追記しませんでした。該当部分を削除して再投稿してください。",
        }
    attributes = {
        **extract_case_attributes(redaction["text"]),
        **{key: value for key, value in structured.items() if key not in {"blocked", "sensitive_flags", "title", "summary"} and value},
    }
    updated_title = str(structured.get("title") or record.get("title", ""))
    merged_summary = merge_case_summary(
        str(record.get("summary", "")),
        str(structured.get("summary") or ""),
        redaction["text"],
        case_id=str(record.get("case_id", "")),
    )
    merged_flags = sorted(set(record.get("sensitive_flags", []) + redaction["flags"]))
    attribute_keywords = [value for value in attributes.values() if isinstance(value, str)]
    merged_flags = sorted(set(merged_flags + structured.get("sensitive_flags", [])))
    merged_keywords = list(dict.fromkeys(record.get("keywords", []) + build_keywords(redaction["text"], attributes) + attribute_keywords))[:30]
    merged_source_urls = merge_external_source_urls(
        record.get("external_source_urls", []),
        [*collect_external_source_urls(redaction["text"]), *current_payload_source_urls(), *current_web_source_urls(), *explicit_source_urls],
    )
    merged_status = "ready" if len(merged_summary) >= 30 else record.get("status", "draft")
    merged_redaction_status = merge_redaction_status(record.get("redaction_status", "safe"), redaction["status"])

    update_names = {"#status": "status"}
    update_values: dict[str, Any] = {
        ":title": updated_title,
        ":summary": merged_summary,
        ":updated_at": now,
        ":status": merged_status,
        ":redaction_status": "masked" if merged_flags else merged_redaction_status,
        ":flags": merged_flags,
        ":keywords": merged_keywords,
    }
    update_parts = [
        "title = :title",
        "summary = :summary",
        "updated_at = :updated_at",
        "#status = :status",
        "redaction_status = :redaction_status",
        "sensitive_flags = :flags",
        "keywords = :keywords",
    ]
    if merged_source_urls:
        update_values[":external_source_urls"] = merged_source_urls
        update_parts.append("external_source_urls = :external_source_urls")
    for index, (key, value) in enumerate(attributes.items()):
        name_key = f"#attr{index}"
        value_key = f":attr{index}"
        update_names[name_key] = key
        update_values[value_key] = value
        update_parts.append(f"{name_key} = {value_key}")

    response = cases_table.update_item(
        Key={"case_id": record["case_id"]},
        UpdateExpression=f"SET {', '.join(update_parts)}",
        ConditionExpression="attribute_exists(case_id)",
        ExpressionAttributeNames=update_names,
        ExpressionAttributeValues=update_values,
        ReturnValues="ALL_NEW",
    )
    updated = response["Attributes"]
    put_case_markdown(updated)
    save_thread_state(updated)
    updated = schedule_case_graphic(updated)

    # 永続化はここまでで完了。以降の返信整形で例外が出てもツールエラーにせず、
    # 保存済みであることが伝わる簡易メッセージで返す（2026-07-09 障害の再発防止）
    try:
        update_lines = [format_case_paragraph(updated)]
        update_lines.extend(format_source_url_bullets(updated))
        notice_lines: list[str] = []
        notice_lines.extend(format_url_fetch_notice_lines())
        if updated.get("redaction_status") == "masked":
            notice_lines.append("※ メールアドレス・電話番号・顧客担当者名などは伏せ字化して保存しました。")
        if updated.get("graphic_status") == "pending":
            notice_lines.append(GRAPHIC_PENDING_NOTICE)
            _graphic_pending_scheduled.get().append(True)

        return with_case_graphic(
            {
                "ok": True,
                "action": "update",
                "case_id": updated["case_id"],
                "slack_text": format_slack_sections(
                    response_prefix or default_update_response_prefix(updated),
                    update_lines,
                    notice_lines,
                ),
            },
            updated,
        )
    except Exception as exc:
        log_event("update_reply_format_failed", case_id=str(updated.get("case_id", "")), error_type=type(exc).__name__)
        return {
            "ok": True,
            "action": "update",
            "case_id": str(updated.get("case_id", "")),
            "slack_text": f"事例を更新し、保存まで完了しています: {updated.get('title', '')}",
        }


def default_update_response_prefix(record: dict[str, Any]) -> str:
    if has_readable_fetched_url_sources():
        return f"URLの内容を読み込み、「{record['title']}」を更新しました。"
    return f"「{record['title']}」を更新しました。"


@tool
def search_cases_tool(query: str, search_terms: list[str] | None = None) -> str:
    """登録済み社内事例を自然文で検索します。

    Args:
        query: 探したいテーマ、部署、技術名、顧客名、課題など。
        search_terms: 検索に使う補助語。業界名、顧客名の別名、技術カテゴリ、用途語を含めます。

    Returns:
        Slack返信用のJSON文字列。近い事例を最大3件返します。
    """
    normalized_query = build_search_query(query)
    if is_help_request(normalized_query):
        return remember_tool_result(
            {
                "ok": True,
                "action": "help",
                "slack_text": format_usage_reply(),
            },
            "search_cases_tool",
        )
    if looks_like_out_of_scope_request(normalized_query):
        return remember_tool_result(
            {
                "ok": True,
                "action": "help",
                "slack_text": format_usage_reply("このBotでできることから少し外れていそうです。"),
            },
            "search_cases_tool",
        )
    terms = normalize_string_list(search_terms or [])
    if KNOWLEDGE_BASE_ID and not _retrieved_case_ids.get():
        retrieve_cases_from_knowledge_base(
            normalized_query,
            CASE_KNOWLEDGE_RETRIEVAL_RESULTS,
            record_call="retrieve_cases_tool" not in _tool_calls.get(),
        )
    records = scan_active_cases()
    semantic_results = semantic_records_from_retrieval(records, normalized_query, terms)
    lexical_results = [item["record"] for item in rank_search_results(records, normalized_query, terms)]
    results = merge_search_records(semantic_results, lexical_results)
    results, confidence = order_results_by_confidence(results, normalized_query, terms)
    save_search_state(normalized_query, results)

    original_question = normalize_slack_text(str(_current_payload.get().get("text", ""))) or query
    # 収集対象（AI事例）と関係なさそうな質問には、未登録の可能性が高いことを正直に伝える
    scope_note = (
        ""
        if looks_like_ai_related_topic(" ".join([original_question, normalized_query, *terms]))
        else AI_SCOPE_NOTE
    )

    if not results:
        not_found_lines = [f"「{normalized_query}」に合う登録済み事例はまだ見つかりませんでした。"]
        if scope_note:
            not_found_lines.append(scope_note)
        # 該当ゼロで打ち切らず、無関係とも言い切れない近傍候補があればヒントとして添える
        hint_records = hint_records_from_retrieval(records, normalized_query, terms)
        if hint_records:
            save_search_state(normalized_query, hint_records)
            not_found_lines.append(
                f"関連度はそこまで高くありませんが、もしかすると{format_title_list(hint_records)}がヒントになる可能性があります。"
                "気になるものがあれば、このスレッドで Bot にメンションして聞いてください。"
            )
        elif not scope_note:
            not_found_lines.append("部署名、技術名、顧客名、課題名などで聞き方を変えると拾えることがあります。")
        return remember_tool_result(
            {
                "ok": True,
                "action": "search",
                "slack_text": format_slack_sections(*not_found_lines),
            },
            "search_cases_tool",
        )

    # 確信できる一致があるなら、スコープ案内は矛盾するので添えない
    lines = format_search_response(
        results,
        original_question,
        terms,
        confidence=confidence,
        scope_note=scope_note if confidence != "strong" else "",
    )

    return remember_tool_result(
        {
            "ok": True,
            "action": "search",
            "slack_text": lines,
            # 断定できない一致に無関係な可能性のあるグラレコ画像を添えない
            "graphics": compact_graphics(results[:1]) if confidence == "strong" else [],
        },
        "search_cases_tool",
    )


def resolve_case_for_detail(case_id: str = "") -> dict[str, Any] | None:
    target_case_id = case_id or ""
    if not target_case_id:
        state = get_current_thread_state(_current_payload.get())
        if state:
            case_ids = state.get("last_search_case_ids") or []
            if isinstance(case_ids, list) and case_ids:
                target_case_id = str(case_ids[0])
            elif state.get("case_id"):
                target_case_id = str(state["case_id"])
    return get_case_by_id(target_case_id) if target_case_id else None


@tool
def get_case_context_tool(case_id: str = "") -> str:
    """深掘り回答の前に、対象事例の登録済み情報を取得します。

    Args:
        case_id: 詳細化したいcase_id。空なら同じSlackスレッドの直前検索結果を使います。

    Returns:
        回答の根拠にしてよい事例情報のJSON文字列。ここにない内容は推測しません。
    """
    record_tool_call("get_case_context_tool")
    record = resolve_case_for_detail(case_id)
    if not record:
        return json.dumps(
            {"ok": False, "error": "case_not_found"},
            ensure_ascii=False,
        )
    allowed_fields = [
        "case_id",
        "title",
        "summary",
        "project_or_customer",
        "customer_industry",
        "department",
        "contact_hint",
        "owner_department",
        "owner_name",
        "team",
        "problem",
        "approach",
        "impact",
        "technologies",
        "tags",
    ]
    context = {field: record[field] for field in allowed_fields if record.get(field)}
    return json.dumps({"ok": True, "case": context}, ensure_ascii=False, default=json_fallback)


@tool
def describe_case_tool(case_id: str = "", question: str = "", answer: str = "") -> str:
    """直前に検索された事例、または指定されたcase_idの詳細を説明します。

    Args:
        case_id: 詳細化したいcase_id。空なら同じSlackスレッドの直前検索結果の1件目を使います。
        question: ユーザーの追加質問。例: 営業向けに説明して、その詳細は、詳細リンクはある、効果は何か。リンク質問の場合は原文の意図を残します。
        answer: get_case_context_tool の結果だけを根拠にした、質問への直接回答。登録情報にない点は分からないと明記し、担当者案内は含めません。

    Returns:
        Slack返信用のJSON文字列。事例の詳細説明を含みます。
    """
    payload = _current_payload.get()
    record = resolve_case_for_detail(case_id)
    if not record:
        return remember_tool_result(
            {
                "ok": False,
                "action": "describe",
                "slack_text": "どの事例の詳細か特定できませんでした。このスレッドで「1番の詳細」のように、直前の検索結果から指定して聞いてください。",
            },
            "describe_case_tool",
        )

    save_thread_state(record)
    original_question = normalize_slack_text(str(payload.get("text", ""))) or normalize_slack_text(question)
    if is_case_detail_link_request(original_question):
        return remember_tool_result(
            {
                "ok": True,
                "action": "describe",
                "case_id": record["case_id"],
                "slack_text": format_case_link_unavailable_reply(record),
            },
            "describe_case_tool",
        )

    return remember_tool_result(
        with_case_graphic(
            {
                "ok": True,
                "action": "describe",
                "case_id": record["case_id"],
                "slack_text": format_case_detail(record, original_question, answer),
            },
            record,
        ),
        "describe_case_tool",
    )


@tool
def case_next_action_tool(case_id: str = "", question: str = "") -> str:
    """事例を見つけた後の進め方や、誰に聞けばよいかを案内します。

    「このあとどうする？」「次のアクションは？」「誰に聞けばいい？」のような、
    社内事例をきっかけに担当者との会話へ進みたい質問で使います。
    これは対象外質問ではなく、このBotの主要な橋渡し機能です。

    Args:
        case_id: 対象のcase_id。空なら同じSlackスレッドに紐づく事例を使います。
        question: ユーザーが尋ねた次の行動・相談先についての質問。

    Returns:
        Slack返信用のJSON文字列。登録済みの社内担当者・部署と、直前に知りたかった観点を使って案内します。
    """
    record = resolve_case_for_detail(case_id)
    if not record:
        return remember_tool_result(
            {
                "ok": False,
                "action": "handoff",
                "slack_text": "どの事例についての相談か特定できませんでした。気になる事例名を添えて、もう一度聞いてください。",
            },
            "case_next_action_tool",
        )

    save_thread_state(record)
    payload = _current_payload.get()
    original_question = normalize_slack_text(str(payload.get("text", ""))) or normalize_slack_text(question)
    context_question = collect_recent_case_questions(payload)
    return remember_tool_result(
        {
            "ok": True,
            "action": "handoff",
            "case_id": record["case_id"],
            "slack_text": format_case_next_action(record, original_question, context_question),
        },
        "case_next_action_tool",
    )


@tool
def get_case_graphic_tool(case_id: str = "") -> str:
    """登録済み事例のグラレコ画像そのものを返します。

    「グラレコできた？」「グラレコある？」「グラレコ見せて」のように、事例の
    グラフィックレコーディング画像を求められたときに使います。生成状況は
    保存済みの graphic_status で確認するので、推測で答えたり、利用者に
    チャンネルを見に行かせたりしないでください。

    Args:
        case_id: 対象のcase_id。空なら同じSlackスレッドに紐づく事例を使います。

    Returns:
        Slack返信用のJSON文字列。生成済みならグラレコ画像を添付します。
    """
    record = resolve_case_for_detail(case_id)
    if not record:
        return remember_tool_result(
            {
                "ok": False,
                "action": "graphic",
                "slack_text": "どの事例のグラレコか特定できませんでした。事例名を添えて、もう一度聞いてください。",
            },
            "get_case_graphic_tool",
        )

    save_thread_state(record)
    status = str(record.get("graphic_status") or "")
    title = str(record.get("title") or "この事例")

    if status == "ready" and record.get("graphic_s3_key"):
        return remember_tool_result(
            with_case_graphic(
                {
                    "ok": True,
                    "action": "graphic",
                    "case_id": record["case_id"],
                    "slack_text": f"「{title}」のグラレコはこちらです！",
                },
                record,
            ),
            "get_case_graphic_tool",
        )

    if status == "pending":
        return remember_tool_result(
            {
                "ok": True,
                "action": "graphic",
                "case_id": record["case_id"],
                "slack_text": (
                    f"「{title}」のグラレコは{format_graphic_pending_eta(record)}。"
                    "出来上がったころに、またこのスレッドで聞いてください！"
                ),
            },
            "get_case_graphic_tool",
        )

    # 生成失敗、または画像が無いままの古いレコード。ここで作り直しを予約して、
    # 「失敗しました」で終わらせない。
    retried = schedule_case_graphic(record)
    if retried.get("graphic_status") != "pending":
        return remember_tool_result(
            {
                "ok": False,
                "action": "graphic",
                "case_id": record["case_id"],
                "slack_text": f"「{title}」のグラレコはまだありません。いまは画像生成を止めているので、こちらで確認します。",
            },
            "get_case_graphic_tool",
        )
    reason = "生成に失敗していました" if status == "failed" else "まだ作っていませんでした"
    return remember_tool_result(
        {
            "ok": True,
            "action": "graphic",
            "case_id": record["case_id"],
            "slack_text": (
                f"「{title}」のグラレコは{reason}。いま作り直しを予約したので、"
                f"{graphic_delay_minutes_text()}ほどしたらまた聞いてください！"
            ),
        },
        "get_case_graphic_tool",
    )


def graphic_delay_minutes_text() -> str:
    return f"{max(1, round(GRAPHIC_GENERATION_DELAY_SECONDS / 60))}分"


def format_graphic_pending_eta(record: dict[str, Any]) -> str:
    """保留中のグラレコが出来上がる目安を、保存済みの予定時刻から組み立てる。"""
    try:
        remaining = int(record.get("graphic_due_epoch", 0)) - epoch_seconds()
    except (TypeError, ValueError):
        remaining = 0
    if remaining <= 30:
        return "いま生成中です"
    return f"生成待ちで、あと{max(1, round(remaining / 60))}分ほどで出来上がります"


@tool
def describe_bot_tool(question: str = "") -> str:
    """このBot自身の設計、モデル、グラレコ生成、検索、情報保護、定期処理を説明します。

    社内事例の内容ではなく、「このBotは何で動いている？」「君のグラレコの文字化け対策は？」
    のように、このアプリ自体について尋ねられた場合に使います。

    Args:
        question: このBot自身についての質問。ユーザーの表現を省略せず渡します。

    Returns:
        実装済みの構成だけを根拠にしたSlack返信用JSON文字列。
    """
    payload = _current_payload.get()
    original_question = resolve_bot_design_question(payload, question)
    addressee = resolve_bot_design_addressee(payload)
    return remember_tool_result(
        {
            "ok": True,
            "action": "bot_info",
            "slack_text": format_bot_design_reply(original_question, addressee=addressee),
        },
        "describe_bot_tool",
    )


@tool
def chat_reply_tool(message: str) -> str:
    """Botの動作・直前の処理・URL読み取り状況についての質問や、軽いやり取りに自然な文章で返信します。

    「URL読めてないの？」「なんでこの内容になったの？」のようなメタ質問には、
    fetched_url_sources / thread_state.last_url_fetch_results の事実に基づいて、
    どの取得手段で読めたか・なぜ読めなかったかを正直に説明してください。

    Args:
        message: Slackにそのまま投稿する自然な日本語の返信文。事実に基づき、推測で断定しない内容にします。

    Returns:
        Slack返信用のJSON文字列。
    """
    prior_results = _tool_results.get()
    if prior_results and prior_results[-1].get("action") in TERMINAL_CASE_REPLY_ACTIONS:
        log_event(
            "chat_reply_ignored_after_terminal_case_reply",
            prior_action=prior_results[-1].get("action"),
        )
        return json.dumps(prior_results[-1], ensure_ascii=False, default=json_fallback)

    # 登録・更新が1件だけのときは、そのツールが組み立てた返信文が完成している。
    # モデルが仕上げのつもりで chat_reply_tool を追加で呼ぶと最終応答がそちらに差し替わり、
    # "placeholder" のような未完成の文字列で塗り潰される（2026-08-03 障害）。
    # まとめ報告が必要な複数事例のときだけ chat_reply_tool を通す。
    case_write_results = successful_case_write_results(prior_results)
    if len(case_write_results) == 1:
        log_event(
            "chat_reply_ignored_after_single_case_write",
            prior_action=case_write_results[-1].get("action"),
            case_id=case_write_results[-1].get("case_id"),
        )
        return json.dumps(case_write_results[-1], ensure_ascii=False, default=json_fallback)

    drive_failure_reply = format_current_drive_failure_reply()
    if drive_failure_reply:
        message = drive_failure_reply

    # 複数事例のまとめ報告でも、本文が穴埋めのままなら Slack へ出さず直前の業務ツール結果へ戻す。
    if looks_like_placeholder_reply(message):
        fallback = case_write_results[-1] if case_write_results else (prior_results[-1] if prior_results else None)
        log_event("chat_reply_placeholder_rejected", has_fallback=fallback is not None)
        if fallback is not None:
            return json.dumps(fallback, ensure_ascii=False, default=json_fallback)
        return remember_tool_result(
            {
                "ok": False,
                "action": "chat",
                "slack_text": "返信文をうまく組み立てられませんでした。少し時間をおいて、もう一度メンションしてもらえると助かります。",
            },
            "chat_reply_tool",
        )

    sanitized = sanitize_slack_text(message)
    if sanitized["status"] == "blocked" or not sanitized["text"].strip():
        return remember_tool_result(
            {
                "ok": False,
                "action": "chat",
                "slack_text": "返信本文に投稿できない情報が含まれていたため、送信を控えました。",
            },
            "chat_reply_tool",
        )
    return remember_tool_result(
        {
            "ok": True,
            "action": "chat",
            "slack_text": sanitized["text"].strip(),
        },
        "chat_reply_tool",
    )


def successful_case_write_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """このターンで実際に保存まで完了した登録・更新の結果だけを返す。"""
    return [
        result
        for result in results
        if isinstance(result, dict) and result.get("ok") and result.get("action") in CASE_WRITE_ACTIONS
    ]


def looks_like_placeholder_reply(message: str) -> bool:
    """本文を書かずに残された穴埋め文字列かどうかを保守的に判定する。

    正当な返信を誤って捨てないよう、記号を除いた実質24文字以下で、かつ
    既知のプレースホルダ語と完全一致する場合だけ true にする。
    """
    normalized = re.sub(r"[\s\W_]+", "", str(message or ""), flags=re.UNICODE).lower()
    if not normalized:
        return True
    if len(normalized) > MAX_PLACEHOLDER_REPLY_CHARS:
        return False
    return normalized in PLACEHOLDER_REPLY_TEXTS


def format_current_drive_failure_reply() -> str:
    if has_readable_fetched_url_sources():
        return ""
    errors = {
        str(source.get("error") or "")
        for source in _fetched_url_sources.get()
        if (urlparse(str(source.get("url") or "")).hostname or "").lower() in GOOGLE_DRIVE_HOSTS
    }
    if "drive_file_not_accessible" in errors:
        return format_slack_sections(
            "ごめん、この資料はBotのアカウントから開けなかった🙏",
            "同じURLの送り直しは不要です。Botから直接アクセスできる状態かだけ確認してもらえると助かります。",
            "急ぎなら資料の要点をこのスレッドに貼ってもらえれば、先に登録できるよ！",
        )
    bot_side_errors = {
        "drive_credentials_unavailable",
        "drive_metadata_fetch_failed",
        "drive_download_failed",
    }
    if not errors.intersection(bot_side_errors):
        return ""
    return format_slack_sections(
        "ごめん、資料の読み取りでBot側のエラーが起きちゃった🙏",
        "同じURLの送り直しは不要です。こちらで確認が必要なエラーです。",
        "急ぎで登録したいときは、資料の要点をこのスレッドに貼ってもらえれば先に登録できるよ！",
    )


@tool
def unsupported_request_tool(question: str = "") -> str:
    """このBotの対象外の質問に、使い方を案内します。

    Args:
        question: ユーザーの質問や依頼文。

    Returns:
        Slack返信用のJSON文字列。現在対応している登録・検索の使い方を案内します。
    """
    payload = _current_payload.get()
    original_question = normalize_slack_text(str(payload.get("text", ""))) or normalize_slack_text(question)
    state = get_current_thread_state(payload)
    if state and not looks_like_out_of_scope_request(original_question):
        record = resolve_case_for_detail()
        if record:
            log_event(
                "unsupported_request_recovered_from_case_context",
                case_id=record.get("case_id"),
                next_action=is_case_next_action_request(original_question),
                detail=is_case_deep_dive_question(original_question),
            )
            if is_case_deep_dive_question(original_question) and not is_case_next_action_request(original_question):
                return describe_case_tool(case_id=str(record["case_id"]), question=original_question)
            return case_next_action_tool(case_id=str(record["case_id"]), question=original_question)

    return remember_tool_result(
        {
            "ok": True,
            "action": "help",
            "slack_text": format_usage_reply("このBotでできることから少し外れていそうです。"),
        },
        "unsupported_request_tool",
    )


@tool
def delete_test_cases_tool(marker: str) -> str:
    """E2E検証用テストデータだけを削除します。

    Args:
        marker: E2E_TEST_ で始まるテストマーカー。

    Returns:
        Slack返信用のJSON文字列。削除件数を含みます。
    """
    if not marker.startswith("E2E_TEST_"):
        return remember_tool_result(
            {
                "ok": False,
                "action": "delete_test",
                "slack_text": "MVP の自動削除は E2E_TEST_ で始まるテストデータだけ対応しています。",
            },
            "delete_test_cases_tool",
        )

    records = [
        record
        for record in scan_active_cases()
        if record.get("test_marker") == marker or marker in record.get("title", "") or marker in record.get("summary", "")
    ]
    for record in records:
        cases_table.delete_item(Key={"case_id": record["case_id"]})
        delete_case_markdown_objects(record["case_id"])
        if record.get("graphic_s3_key"):
            s3.delete_object(Bucket=CASE_ASSETS_BUCKET_NAME, Key=record["graphic_s3_key"])
        delete_thread_states_for_case(record["case_id"])

    return remember_tool_result(
        {
            "ok": True,
            "action": "delete_test",
            "deleted_count": len(records),
            "slack_text": f"テストデータ {marker} を削除しました。削除件数: {len(records)}",
        },
        "delete_test_cases_tool",
    )


@tool
def pick_daily_share_tool() -> str:
    """日次共有する事例を1件選びます。

    Returns:
        Slackチャンネル投稿用のJSON文字列。共有候補がなければ post_to_channel=false を返します。
    """
    candidates = [
        record
        for record in scan_active_cases()
        if record.get("status") == "ready"
    ]
    candidates.sort(key=lambda record: record.get("last_shared_at", ""))
    if not candidates:
        return remember_tool_result(
            {
                "ok": True,
                "action": "daily_share",
                "post_to_channel": False,
                "slack_text": "",
            },
            "pick_daily_share_tool",
        )

    record = candidates[0]
    return remember_tool_result(
        with_case_graphic(
            {
                "ok": True,
                "action": "daily_share",
                "post_to_channel": True,
                "case_id": record["case_id"],
                "slack_text": format_daily_share(record),
            },
            record,
        ),
        "pick_daily_share_tool",
    )


@app.entrypoint
async def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    _current_payload.set(payload)
    _tool_calls.set([])
    _tool_results.set([])
    _web_source_urls.set([])
    _fetched_url_sources.set([])
    _retrieved_case_ids.set([])
    _retrieval_results.set([])
    _graphic_pending_scheduled.set([])

    direct_result = handle_direct_mode(payload)
    if direct_result is not None:
        return with_agent_metadata(direct_result)

    if payload.get("mode") == "graphic_maintenance":
        return with_agent_metadata(process_pending_graphics())
    if payload.get("mode") == "knowledge_base_sync":
        return with_agent_metadata(process_knowledge_base_sync())

    bot_design_result = handle_bot_design_request(payload)
    if bot_design_result is not None:
        return with_agent_metadata(bot_design_result)

    graphic_result = handle_case_graphic_request(payload)
    if graphic_result is not None:
        return with_agent_metadata(graphic_result)

    contextual_result = handle_contextual_case_request(payload)
    if contextual_result is not None:
        return with_agent_metadata(contextual_result)

    prompt = build_prompt(payload, context)
    try:
        result_text = await run_agent(prompt)
        tool_results = _tool_results.get()
        if tool_results:
            return with_agent_metadata(append_graphic_notice_if_needed(tool_results[-1]))

        parsed = parse_json_from_text(result_text)
        business_tool_calls = [tool_name for tool_name in _tool_calls.get() if tool_name not in HELPER_TOOL_NAMES]
        if parsed:
            if business_tool_calls:
                log_event(
                    "agent_tool_result_from_final_response",
                    action=parsed.get("action"),
                    ok=parsed.get("ok"),
                    case_id=parsed.get("case_id"),
                    tool_calls=_tool_calls.get(),
                )
                return with_agent_metadata(append_graphic_notice_if_needed(parsed))

            log_event("agent_returned_without_tool", action=parsed.get("action"), ok=parsed.get("ok"))
            return with_agent_metadata(agent_routing_error_reply())

        log_event("agent_no_tool_call", mode=payload.get("mode"), tool_calls=_tool_calls.get())
        if business_tool_calls:
            return with_agent_metadata(tool_result_tracking_error_reply())
        return with_agent_metadata(agent_routing_error_reply())
    except Exception as exc:
        logger.error(
            json.dumps(
                {"level": "error", "message": "agent_invocation_failed", "error": str(exc)[:500]},
                ensure_ascii=False,
            )
        )
        return {
            "ok": False,
            "action": "error",
            "slack_text": "AgentCore Runtime 側で処理中にエラーが起きました。本文やトークンはログに残さず、イベント情報だけで確認します。",
            "error": type(exc).__name__,
        }


def handle_direct_mode(payload: dict[str, Any]) -> dict[str, Any] | None:
    mode = payload.get("mode")
    if mode == "image_canary":
        return run_image_canary(payload)
    if mode == "drive_canary":
        return run_drive_canary(payload)
    if mode == "generate_pending_images":
        return {
            **process_pending_graphics(),
            "post_to_channel": False,
        }
    return None


def handle_bot_design_request(payload: dict[str, Any]) -> dict[str, Any] | None:
    """このBot自身への質問は、モデルのツール分類に依存せず実装事実から答える。"""
    if payload.get("mode", "slack_app_mention") != "slack_app_mention":
        return None
    question = normalize_slack_text(str(payload.get("text", "")))
    if not is_bot_design_question(question, payload.get("thread_messages")):
        return None

    result = parse_json_from_text(describe_bot_tool(question=question))
    if result:
        resolved_question = resolve_bot_design_question(payload, question)
        log_event("bot_design_question_routed", topics=bot_design_topics(resolved_question))
        return result
    return tool_result_tracking_error_reply()


def handle_case_graphic_request(payload: dict[str, Any]) -> dict[str, Any] | None:
    """グラレコ画像の要求は、モデルのツール分類に依存せず保存済みの状態から答える。

    2026-08-04、「グラレコできた？」が chat_reply_tool に流れ、確認できるはずの
    生成状況を推測で答えたうえ、投稿されるはずのないチャンネルを利用者に
    見に行かせる返信になった。同じ経路をコードで塞ぐ。
    """
    if payload.get("mode", "slack_app_mention") != "slack_app_mention":
        return None
    question = normalize_slack_text(str(payload.get("text", "")))
    if not is_case_graphic_request(question):
        return None
    if not extract_case_id(question):
        state = get_current_thread_state(payload)
        if not state or not (state.get("case_id") or state.get("last_search_case_ids")):
            return None

    result = parse_json_from_text(get_case_graphic_tool(case_id=extract_case_id(question) or ""))
    if result:
        log_event("case_graphic_request_routed", case_id=result.get("case_id"), ok=result.get("ok"))
        return result
    return tool_result_tracking_error_reply()


def handle_contextual_case_request(payload: dict[str, Any]) -> dict[str, Any] | None:
    """事例発見後の次アクション相談は、モデルのツール分類に依存せず橋渡しする。"""
    if payload.get("mode", "slack_app_mention") != "slack_app_mention":
        return None
    question = normalize_slack_text(str(payload.get("text", "")))
    if not is_case_next_action_request(question) or looks_like_out_of_scope_request(question):
        return None
    state = get_current_thread_state(payload)
    if not state or not (state.get("case_id") or state.get("last_search_case_ids")):
        return None

    result = parse_json_from_text(case_next_action_tool(question=question))
    if result:
        log_event("case_next_action_routed_from_thread_context", case_id=result.get("case_id"))
        return result
    return tool_result_tracking_error_reply()


def run_drive_canary(payload: dict[str, Any]) -> dict[str, Any]:
    """Slackへ投稿せず、Drive OAuth・共有設定・本文抽出を本番経路で確認する。"""
    request_url = str(payload.get("url") or "").strip()
    source = fetch_url_source(request_url, 1_000) if request_url else {
        "ok": False,
        "error": "url_required",
        "attempts": [],
    }
    excerpt = str(source.get("content_excerpt") or "")
    result = {
        "ok": bool(source.get("ok")),
        "action": "drive_canary",
        "post_to_channel": False,
        "url": source.get("url") or normalize_external_source_url(request_url),
        "method": source.get("method"),
        "chars": len(excerpt),
        "warnings": source.get("warnings") or [],
        "attempts": source.get("attempts") or [],
        "slack_text": "",
    }
    if source.get("error"):
        result["error"] = source["error"]
    log_event(
        "drive_canary_completed",
        ok=result["ok"],
        method=result["method"],
        chars=result["chars"],
        error=result.get("error"),
    )
    return result


def run_image_canary(payload: dict[str, Any]) -> dict[str, Any]:
    case_id = str(payload.get("case_id") or "")
    record = get_case_by_id(case_id) if case_id else None
    if record is None:
        record = {
            "case_id": f"canary-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}",
            "title": "画像生成 canary",
            "summary": "Google Cloud の Vertex AI 認証と画像生成を確認するための検証用カードです。",
            "project_or_customer": "事例共有くん",
            "customer_industry": "社内業務",
            "department": "社内AI推進チーム",
            "problem": "API key を使わずに本番 Runtime から画像生成できるか確認する。",
            "approach": "AWS AgentCore Runtime から Workload Identity Federation で Vertex AI Gemini 画像生成モデルを呼び出す。",
            "impact": "Slack へ投稿せず、S3 に画像が保存できることだけを確認する。",
            "technologies": ["Vertex AI", "Gemini", "Workload Identity Federation", "AgentCore"],
            "tags": ["canary", "image-generation"],
        }

    image_bytes, mime_type = generate_case_graphic(record)
    key = f"{IMAGE_ASSET_PREFIX}/canary/{record['case_id']}.{image_extension(mime_type)}"
    s3.put_object(
        Bucket=CASE_ASSETS_BUCKET_NAME,
        Key=key,
        ContentType=mime_type,
        Body=image_bytes,
        Metadata={"case-id": record["case_id"], "model": GEMINI_IMAGE_MODEL, "purpose": "canary"},
    )
    log_event(
        "image_canary_generated",
        case_id=record["case_id"],
        image_s3_key=key,
        model=GEMINI_IMAGE_MODEL,
        google_project=GOOGLE_CLOUD_PROJECT,
        google_location=GOOGLE_CLOUD_LOCATION,
    )
    return {
        "ok": True,
        "action": "image_canary",
        "post_to_channel": False,
        "case_id": record["case_id"],
        "image_s3_key": key,
        "image_mime_type": mime_type,
        "image_model": GEMINI_IMAGE_MODEL,
        "google_project": GOOGLE_CLOUD_PROJECT,
        "google_location": GOOGLE_CLOUD_LOCATION,
        "slack_text": "",
    }


def json_fallback(value: Any) -> Any:
    """json.dumps の default。DynamoDB 由来の Decimal 等が紛れても invoke を落とさない安全弁。"""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return str(value)


def build_prompt(payload: dict[str, Any], context: Any = None) -> str:
    mode = payload.get("mode", "slack_app_mention")
    text = normalize_slack_text(str(payload.get("text", "")))
    thread_state = summarize_thread_state(get_current_thread_state(payload))
    thread_messages = summarize_thread_messages(payload.get("thread_messages", []))
    fetched_url_sources = fetch_url_sources_for_prompt(text, payload.get("thread_messages"))
    _fetched_url_sources.set(fetched_url_sources)
    safe_payload = {
        "mode": mode,
        "text": text,
        "channel_id": payload.get("channel_id"),
        "thread_ts": payload.get("thread_ts"),
        "message_ts": payload.get("message_ts"),
        "user_id": payload.get("user_id"),
        "session_id": getattr(context, "session_id", None) if context else None,
        "thread_state": thread_state,
        "thread_messages": thread_messages,
        "fetched_url_sources": fetched_url_sources,
    }
    return "\n".join(
        [
            "以下のSlackイベントを処理してください。",
            "本文とmodeを読んで、登録・追記・検索・詳細確認・担当者への橋渡し・テスト削除・日次共有のどれかを判断し、最後は必ず該当する業務ツールを呼び出してください。通常は1つで十分です。",
            "URLの資料に互いに独立した事例が複数含まれる場合は、事例ごとに register_case_tool（または重複時は update_case_tool）を複数回呼び出して構いません。ただしツールは1回の応答につき必ず1つだけ呼び出し、結果を確認してから次の事例のツールを呼んでください（複数のツール呼び出しを1つの応答にまとめると途中で切れます）。2件以上を処理した場合だけ、最後に chat_reply_tool で登録・更新した事例タイトルの一覧、件数、確認が必要な事例を1つの返信にまとめて報告してください。登録・更新が1件だけなら chat_reply_tool は呼ばず、そのツールの結果をそのまま最終応答にしてください。",
            "新規登録の前に、事例のタイトル・顧客名・案件名をクエリにして retrieve_cases_tool で既存事例との重複を確認してください。明確に同一の取り組みが既にあれば register_case_tool ではなく update_case_tool で追記してください。register_case_tool は登録直前にも重複チェックを行い、疑いがあると action=duplicate_suspected と候補一覧を返して登録を保留します。その場合は候補と見比べ、同一なら update_case_tool、判断がつかなければ duplicate_suspected の slack_text の内容を返信に含めて人間に確認してください。ユーザーが『別件です』と明言した時だけ allow_duplicate=true で再登録してください。",
            "更新は既存事例の内容を置き換える操作です。対象を取り違えるとその事例の情報が失われるため、どの事例を更新するか確信が持てないときは、勝手に決めずに必ず利用者へ確認してください。update_case_tool が action=update_pending と候補一覧を返したら、それは対象を確定できなかったという意味です。別の言い方で呼び直したり、候補から自分で選んだりせず、その slack_text をそのまま返信に含めて利用者に選んでもらってください。返ってきた action=update の case_id とタイトルが自分の意図した対象と違う場合も、成功として扱わず利用者へ報告してください。",
            "会社名や具体キーワードから公開Web情報を補うとよい場合だけ、業務ツールの前に tavily_search_tool を使ってください。",
            "検索系の質問では、search_cases_tool の前に retrieve_cases_tool を使ってKnowledge Baseから候補case_idを取得してください。",
            "thread_state.pending_action が update_case の場合、本文が短くても検索ではなく confirm_pending_update_tool を呼び出してください。",
            "thread_state.case_id があるスレッドで追記・更新依頼が来た場合は、そのcase_idを既定の更新対象として update_case_tool を呼び出してください。ただし、ユーザーが別件・他の案件・具体的な別案件名や別case_idを明示した場合は、その明示対象を優先してください。",
            "thread_state.last_search_case_ids または thread_state.case_id があり、本文が「その詳細は？」「詳しく」「営業向けに説明して」のような追質問なら、先に get_case_context_tool で登録情報を読み、その結果だけを根拠に describe_case_tool を呼び出してください。",
            "thread_state.last_search_case_ids または thread_state.case_id がある会話で、「このあとどうする？」「誰に聞けばいい？」「次のアクションは？」「どう進めるのがよい？」のように発見後の進め方を聞かれたら、case_next_action_tool を呼び出してください。これは事例共有の主要な目的である担当者との橋渡しであり、unsupported_request_tool の対象ではありません。質問に人名が含まれる場合は、その人が次に取る行動として自然に答えてください。",
            "describe_case_tool の answer は質問へ直接答える自然な文章にしてください。登録情報にない点は『登録情報では分からない』と正直に伝え、同じ概要を言い換えて穴埋めしたり、推測で補ったりしないでください。担当者への案内はツール側で付けるため answer には含めません。",
            "search_cases_tool、describe_case_tool、case_next_action_tool のいずれかを呼び出した後は、chat_reply_tool など別の業務ツールを追加で呼ばず、そのツール結果を最終応答としてそのまま返してください。",
            "直前の検索に対する「もっと汎用的なもの」「ほかの事例」「別の案件」は詳細質問ではなく検索条件の変更です。retrieve_cases_tool の後に search_cases_tool を呼び出してください。",
            "直前の事例の詳細URL・詳細リンクを聞かれた場合も describe_case_tool を呼び、question にはその質問を省略せず渡してください。この場合は get_case_context_tool を省略して構いません。実在するリンクを推測しないでください。",
            "thread_messages は同じSlackスレッドでBotがメンションされた時だけ渡される補助文脈です。必要な場合だけ参照してください。",
            "fetched_url_sources はSlack本文内URLを多段フォールバック（Tavily Extract -> http_request -> AgentCore Browser）で取得した結果です。method にどの手段で読めたか、attempts に各手段の成否、warnings に会員限定・ログイン必須・本文が薄い等の注意が入ります。ok=true の content_excerpt は、ブログ・登壇資料・公開ページの内容として登録/更新/検索の判断に反映してください。",
            "URL付きの登録・更新依頼では、ユーザーの「このURLで更新して」などの命令文ではなく、ok=true の content_excerpt から読み取った内容を主材料にしてください。",
            "Google Drive / Docs / Slides / Sheets のURLは社内OAuth権限（google_drive_api）で読み取ります。Botへのメンション内でURLを渡すこと、またはメンション時に同一スレッドのURL資料を参照するよう依頼することを、その資料を社内事例へ利用する明示的な依頼として扱い、Driveの共有範囲をBot側で再審査しません。drive_file_not_accessible の場合だけ、BotのOAuthでは資料を開けないことをカジュアルに伝えてください。同じURLの再送や時間待ちは案内しないでください。",
            "Slackへのファイル直接添付は取り込み対象外です。資料を読ませたい依頼があれば、BotのOAuthからアクセスできるGoogle Driveへ置き、そのURLを送ってもらうよう案内してください。",
            "URL内容で更新できる場合は、update_case_tool の additional_content と summary/problem/approach/impact/technologies/tags に、URLから要約した更新後の内容を渡してください。対象事例が thread_state.case_id で分かる場合は確認待ちにせず更新してください。",
            "URL取得が失敗した場合や warnings がある場合（会員限定コンテンツ等）は、そのURL内容を推測せず本文だけで処理し、登録・更新の返信文の中で「URLは会員限定のため読めなかった」等の状況を一言伝えてください。読めなかった理由は attempts / warnings を根拠にしてください。",
            "Botの動作やURL読み取り状況への質問には chat_reply_tool で、fetched_url_sources と thread_state.last_url_fetch_results を根拠に自然な文章で答えてください。",
            "このBot自身のモデル、プロンプト上の工夫、グラレコ生成、設計、検索方式、情報保護、定期処理についての質問には describe_bot_tool を呼び出してください。社内事例の中で使われたモデルや技術を尋ねる質問は、事例検索・詳細確認として扱ってください。",
            "unsupported_request_tool は、天気・翻訳・コード作成など事例共有と明確に無関係な依頼だけに使ってください。事例が紐づくスレッドの曖昧な追質問を対象外扱いして、機能一覧を返してはいけません。",
            "取得本文の生コピーを長く保存せず、社内事例として必要な課題・取り組み・効果・技術・タグに要約してツールへ渡してください。",
            "tavily_search_tool の結果は公開情報の補足です。顧客の業界・製品カテゴリ・公開技術背景など、確実な範囲だけを業界・用途・タグ・概要にうっすら反映し、社内情報や秘密情報の推測には使わないでください。",
            "tavily_search_tool を使った場合、参照した公開URLを register_case_tool / update_case_tool の external_source_urls に渡して内部 traceability には残してよいですが、Slack表示文には参考URLを書かないでください。",
            "登録時は register_case_tool に title / summary / project_or_customer / customer_industry / department / contact_hint / problem / approach / impact / technologies / tags を可能な範囲で渡してください。",
            "summary は何をした事例かが伝わる1〜2つの完結文にし、全体をおおむね120文字以内に収めてください。文の途中で切ったり、末尾を「…」で省略したりしないでください。",
            "曖昧な場合は、保存せずに search_cases_tool を使って既存事例を探してください。",
            "ツール結果のJSONをそのまま最終応答にしてください。",
            json.dumps(safe_payload, ensure_ascii=False, default=json_fallback),
        ]
    )


def fetch_url_sources_for_prompt(text: str, thread_messages: Any = None) -> list[dict[str, Any]]:
    if not EXTERNAL_URL_FETCH_ENABLED:
        return []
    urls = collect_urls_from_message_and_thread(text, thread_messages)[:MAX_FETCH_URLS]
    if not urls:
        return []

    sources: list[dict[str, Any]] = []
    total_chars = 0
    for url in urls:
        remaining_chars = MAX_TOTAL_FETCHED_SOURCE_CHARS - total_chars
        if remaining_chars <= 0:
            break
        host = (urlparse(url).hostname or "").lower()
        per_source_cap = MAX_DOCUMENT_SOURCE_CHARS if host in GOOGLE_DRIVE_HOSTS else MAX_FETCHED_SOURCE_CHARS
        source = fetch_url_source(url, min(per_source_cap, remaining_chars))
        if isinstance(source.get("content_excerpt"), str):
            total_chars += len(source["content_excerpt"])
        sources.append(source)
    return sources


def collect_urls_from_message_and_thread(text: str, thread_messages: Any) -> list[str]:
    """メンション本文のURLを優先しつつ、スレッド内の過去メッセージのURLも拾う。

    「上の◯◯さんが貼った資料を読んで」のようにURLを貼り直さない依頼に
    対応するため、スレッド文脈（新しいメッセージ優先）からも抽出する。
    Botの過去返信に含まれるURLは対象にしない。
    """
    urls = extract_urls(text)
    if isinstance(thread_messages, list):
        for message in reversed(thread_messages):
            if not isinstance(message, dict):
                continue
            if message.get("bot_id") or message.get("is_current"):
                continue
            for url in extract_urls(str(message.get("text", ""))):
                if url not in urls:
                    urls.append(url)
    return urls


def attachment_format(name: str, mimetype: str) -> str:
    mime_map = {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/gif": "gif",
        "image/webp": "webp",
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/msword": "doc",
        "application/vnd.ms-excel": "xls",
        "text/plain": "txt",
        "text/csv": "csv",
        "text/html": "html",
        "text/markdown": "md",
    }
    normalized_mime = mimetype.split(";", 1)[0].strip().lower()
    if normalized_mime in mime_map:
        return mime_map[normalized_mime]
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {"jpg": "jpeg"}.get(extension, extension)


def extract_text_from_binary(data: bytes, fmt: str, name: str) -> tuple[str, str]:
    """バイナリを形式別に読み取り (method, text) を返す。Slack添付とDriveダウンロードで共用。"""
    if fmt in IMAGE_ATTACHMENT_FORMATS:
        return "converse_image", extract_text_from_attachment_image(data, fmt, name)
    if fmt == "pptx":
        return "python_pptx", extract_text_from_pptx_bytes(data)
    if fmt in CONVERSE_DOCUMENT_FORMATS:
        method = ""
        text = ""
        if len(data) <= CONVERSE_DOCUMENT_MAX_BYTES:
            method = "converse_document"
            text = extract_text_from_document_bytes(data, fmt, name)
        if not text and fmt == "pdf":
            method = "pymupdf"
            text = extract_text_from_pdf_bytes(data)
        return method, text
    return "", ""


def extract_text_from_attachment_image(data: bytes, fmt: str, name: str) -> str:
    try:
        response = bedrock_runtime.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "text": (
                                "以下はSlackに添付された画像です。書かれているテキスト内容"
                                "（タイトル、課題、取り組み、効果、技術要素など）を、"
                                "日本語で忠実に書き起こしてください。装飾やレイアウトの説明は不要です。"
                                f" ファイル名: {name}"
                            )
                        },
                        {"image": {"format": fmt, "source": {"bytes": data}}},
                    ],
                }
            ],
            inferenceConfig={"maxTokens": 2000},
        )
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        return "\n".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict)).strip()
    except Exception as exc:
        log_event("attachment_image_extraction_failed", error_type=type(exc).__name__)
        return ""


def extract_text_from_document_bytes(data: bytes, fmt: str, name: str) -> str:
    try:
        response = bedrock_runtime.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "text": (
                                "以下はSlackに添付された資料です。書かれているテキスト内容"
                                "（タイトル、課題、取り組み、効果、技術要素、表の内容など）を、"
                                "日本語で忠実に書き起こしてください。装飾やレイアウトの説明は不要です。"
                            )
                        },
                        {
                            "document": {
                                "format": fmt,
                                "name": converse_document_name(name),
                                "source": {"bytes": data},
                            }
                        },
                    ],
                }
            ],
            inferenceConfig={"maxTokens": 4000},
        )
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        return "\n".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict)).strip()
    except Exception as exc:
        log_event("attachment_document_extraction_failed", format=fmt, error_type=type(exc).__name__)
        return ""


def converse_document_name(name: str) -> str:
    # Converse の document.name は英数字・スペース・ハイフン等しか許されない
    base = name.rsplit(".", 1)[0] if "." in name else name
    cleaned = re.sub(r"[^a-zA-Z0-9\-]+", " ", base).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned[:60] or "attachment"


def extract_text_from_pptx_bytes(data: bytes) -> str:
    try:
        from pptx import Presentation

        presentation = Presentation(io.BytesIO(data))
        pages: list[str] = []
        for index, slide in enumerate(presentation.slides, start=1):
            parts: list[str] = []
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False):
                    frame_text = shape.text_frame.text.strip()
                    if frame_text:
                        parts.append(frame_text)
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                        if cells:
                            parts.append(" | ".join(cells))
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    parts.append(f"(ノート) {notes}")
            if parts:
                pages.append(f"--- スライド{index} ---\n" + "\n".join(parts))
        return "\n\n".join(pages).strip()
    except Exception as exc:
        log_event("attachment_pptx_extraction_failed", error_type=type(exc).__name__)
        return ""


def extract_text_from_pdf_bytes(data: bytes) -> str:
    try:
        import fitz

        document = fitz.open(stream=data, filetype="pdf")
        pages = []
        for index, page in enumerate(document, start=1):
            page_text = page.get_text().strip()
            if page_text:
                pages.append(f"--- ページ{index} ---\n{page_text}")
        return "\n\n".join(pages).strip()
    except Exception as exc:
        log_event("attachment_pdf_extraction_failed", error_type=type(exc).__name__)
        return ""


_google_drive_access_token = ""
_google_drive_token_expires_at = 0


def extract_google_drive_file_id(request_url: str) -> str:
    match = re.search(r"/(?:file|document|presentation|spreadsheets)/d/([A-Za-z0-9_-]{10,})", request_url)
    if match:
        return match.group(1)
    query = parse_qs(urlparse(request_url).query)
    for value in query.get("id", []):
        if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
            return value
    return ""


def get_google_drive_access_token() -> str:
    global _google_drive_access_token, _google_drive_token_expires_at
    if _google_drive_access_token and epoch_seconds() < _google_drive_token_expires_at - 60:
        return _google_drive_access_token

    response = ssm.get_parameter(Name=GOOGLE_DRIVE_OAUTH_PARAMETER_NAME, WithDecryption=True)
    credentials = json.loads(response.get("Parameter", {}).get("Value", "") or "{}")
    for key in ("client_id", "client_secret", "refresh_token"):
        if not credentials.get(key):
            raise RuntimeError(f"google_drive_oauth_parameter_missing_{key}")

    request = urlrequest.Request(
        "https://oauth2.googleapis.com/token",
        data=urlencode(
            {
                "client_id": credentials["client_id"],
                "client_secret": credentials["client_secret"],
                "refresh_token": credentials["refresh_token"],
                "grant_type": "refresh_token",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlrequest.urlopen(request, timeout=15) as token_response:
        payload = json.loads(token_response.read().decode("utf-8"))
    access_token = str(payload.get("access_token") or "")
    if not access_token:
        raise RuntimeError("google_drive_access_token_unavailable")
    _google_drive_access_token = access_token
    _google_drive_token_expires_at = epoch_seconds() + int(payload.get("expires_in") or 3600)
    return access_token


def google_drive_api_get(path_and_query: str, token: str, timeout: int = 30) -> bytes:
    request = urlrequest.Request(
        f"https://www.googleapis.com/drive/v3/{path_and_query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urlrequest.urlopen(request, timeout=timeout) as response:
        return response.read()


def try_google_drive_fetch(request_url: str) -> dict[str, Any]:
    file_id = extract_google_drive_file_id(request_url)
    if not file_id:
        return {"ok": False, "error": "drive_file_id_not_found"}

    try:
        token = get_google_drive_access_token()
    except Exception as exc:
        log_event("drive_credentials_unavailable", error_type=type(exc).__name__)
        return {"ok": False, "error": "drive_credentials_unavailable"}

    record_tool_call("google_drive_fetch")
    try:
        meta = json.loads(
            google_drive_api_get(
                f"files/{file_id}?fields=id,name,mimeType,size&supportsAllDrives=true",
                token,
            ).decode("utf-8")
        )
    except urlerror.HTTPError as exc:
        return {"ok": False, "error": "drive_file_not_accessible", "status_code": exc.code}
    except Exception as exc:
        log_event("drive_metadata_fetch_failed", error_type=type(exc).__name__)
        return {"ok": False, "error": "drive_metadata_fetch_failed"}

    mime_type = str(meta.get("mimeType") or "")
    name = str(meta.get("name") or "drive-file")
    try:
        if mime_type == "application/vnd.google-apps.spreadsheet":
            text = google_drive_api_get(f"files/{file_id}/export?mimeType=text/csv", token).decode("utf-8", "replace")
        elif mime_type.startswith("application/vnd.google-apps."):
            text = google_drive_api_get(f"files/{file_id}/export?mimeType=text/plain", token).decode("utf-8", "replace")
        else:
            data = google_drive_api_get(f"files/{file_id}?alt=media&supportsAllDrives=true", token, timeout=60)
            if len(data) > MAX_ATTACHMENT_BYTES:
                return {"ok": False, "error": "drive_file_too_large"}
            _, text = extract_text_from_binary(data, attachment_format(name, mime_type), name)
    except urlerror.HTTPError as exc:
        return {"ok": False, "error": "drive_download_failed", "status_code": exc.code}
    except Exception as exc:
        log_event("drive_download_failed", error_type=type(exc).__name__)
        return {"ok": False, "error": "drive_download_failed"}

    log_event("drive_fetch_completed", mime_type=mime_type, chars=len(text))
    return {"ok": True, "text": text, "status_code": 200}


def fetch_url_source(raw_url: str, max_chars: int) -> dict[str, Any]:
    """URLの本文を多段フォールバックで取得する。

    取得順: Tavily Extract -> Strands http_request -> AgentCore Browser。
    スライド共有サイト（Speaker Deck等）はテキスト抽出が薄いため、最初から
    AgentCore Browser でレンダリングし、必要ならスクリーンショットを
    Bedrockのマルチモーダル読み取りにかける。
    どの手段で読めたか・なぜ読めなかったかを attempts / warnings に残し、
    Slack返信やメタ質問への説明に使えるようにする。
    """
    request_url = normalize_fetch_request_url(raw_url)
    display_url = normalize_external_source_url(request_url)
    is_valid, reason = validate_fetch_url(request_url)
    if not is_valid:
        return {
            "url": display_url,
            "ok": False,
            "error": reason,
            "attempts": [],
        }

    host = (urlparse(request_url).hostname or "").lower()
    slide_mode = host in SLIDE_HOSTS
    log_event("url_fetch_started", host=host, url=display_url, slide_mode=slide_mode)

    attempts: list[dict[str, Any]] = []
    fallback_candidate: dict[str, Any] | None = None

    def register_attempt(method: str, outcome: dict[str, Any]) -> dict[str, Any]:
        nonlocal fallback_candidate
        text = str(outcome.get("text") or "")
        assessment = assess_source_text(
            text,
            detect_access_wall=method not in TRUSTED_DOCUMENT_FETCH_METHODS,
        )
        attempt = {
            "method": method,
            "ok": bool(outcome.get("ok")),
            "readable": bool(outcome.get("ok")) and assessment["readable"],
            "chars": len(text),
        }
        if outcome.get("error"):
            attempt["error"] = outcome["error"]
        if outcome.get("status_code") is not None:
            attempt["status_code"] = outcome["status_code"]
        if assessment["warnings"]:
            attempt["warnings"] = assessment["warnings"]
        attempts.append(attempt)
        log_event("url_fetch_attempt", url=display_url, **{k: v for k, v in attempt.items()})
        if outcome.get("ok") and text and (fallback_candidate is None or len(text) > len(str(fallback_candidate.get("text") or ""))):
            fallback_candidate = {"method": method, **outcome, "warnings": assessment["warnings"]}
        if attempt["readable"]:
            return {"method": method, **outcome, "warnings": assessment["warnings"]}
        return {}

    def finalize(chosen: dict[str, Any]) -> dict[str, Any]:
        sanitized = sanitize_fetched_source_text(strip_markdown_noise(str(chosen.get("text") or "")))
        content_excerpt = truncate(sanitized["text"], max_chars)
        result = {
            "url": display_url,
            "ok": bool(content_excerpt),
            "method": chosen.get("method"),
            "status_code": chosen.get("status_code"),
            "warnings": chosen.get("warnings") or [],
            "attempts": attempts,
            "redaction_status": sanitized["status"],
            "sensitive_flags": sanitized["flags"],
            "content_excerpt": content_excerpt,
        }
        if not content_excerpt:
            result["error"] = "empty_body"
        log_event(
            "url_fetch_completed",
            host=host,
            method=result["method"],
            ok=result["ok"],
            chars=len(content_excerpt),
            warnings=result["warnings"] or None,
            redaction_status=sanitized["status"],
        )
        return result

    # Google Drive 系URLは、任意機能が有効な場合だけ OAuth（drive.readonly）で取得する。
    # ログイン壁があるため後段の Tavily / http / Browser は意味がなく、短絡する
    if host in GOOGLE_DRIVE_HOSTS:
        if not GOOGLE_DRIVE_ENABLED:
            return {
                "url": display_url,
                "ok": False,
                "error": "google_drive_integration_disabled",
                "attempts": attempts,
            }
        chosen = register_attempt("google_drive_api", try_google_drive_fetch(request_url))
        if chosen:
            return finalize(chosen)
        if fallback_candidate is not None:
            return finalize(fallback_candidate)
        drive_error = next(
            (attempt.get("error") for attempt in reversed(attempts) if attempt.get("error")), "unreadable_content"
        )
        log_event("url_fetch_failed", host=host, error=drive_error)
        return {
            "url": display_url,
            "ok": False,
            "error": drive_error,
            "attempts": attempts,
        }

    # URLが直接PDFを指す場合は、Slack添付ファイルと同じ document ブロック経路で読む。
    # Tavily / http_request はPDFバイナリを扱えず、画像だけのPDF（スキャン等）は
    # テキスト抽出も効かないため、先にマルチモーダル読み取りへ乗せる
    if url_points_to_pdf(request_url):
        chosen = register_attempt("pdf_document", try_pdf_document_fetch(request_url))
        if chosen:
            return finalize(chosen)

    # 軽い順に試す。Speaker Deck等もHTML内の書き起こしテキストが前段で取れることが多い
    chosen = register_attempt("tavily_extract", try_tavily_extract(request_url))
    if chosen:
        return finalize(chosen)
    chosen = register_attempt("http_request", try_http_request_fetch(request_url))
    if chosen:
        return finalize(chosen)

    # 前段で本文が取れなかった場合の最終手段。スライド系はページ送りしながら
    # スクリーンショットを撮り、Bedrockのマルチモーダル読み取りにかける
    if URL_FETCH_BROWSER_ENABLED:
        chosen = register_attempt("agentcore_browser", run_browser_fetch(request_url, slide_mode))
        if chosen:
            return finalize(chosen)

    # 十分な本文は取れなかったが、部分的に読めたテキストがあれば警告付きで返す
    if fallback_candidate is not None:
        return finalize(fallback_candidate)

    last_error = next((attempt.get("error") for attempt in reversed(attempts) if attempt.get("error")), "unreadable_content")
    log_event("url_fetch_failed", host=host, error=last_error)
    return {
        "url": display_url,
        "ok": False,
        "error": last_error,
        "attempts": attempts,
    }


def strip_markdown_noise(text: str) -> str:
    """取得したMarkdown/テキストからナビゲーション由来のノイズを削る。

    Tavily Extract や http_request の markdown 変換結果は、画像・リンクの
    URL がナビゲーション部分に大量に入り、文字数制限内から本文を
    押し出してしまうため、リンクはテキストだけ残して圧縮する。
    """
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    cleaned = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cleaned)
    cleaned = re.sub(r"https?://[^\s)\"']+", " ", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def assess_source_text(text: str, *, detect_access_wall: bool = True) -> dict[str, Any]:
    warnings: list[str] = []
    sample = text[:4000]
    if detect_access_wall:
        if MEMBERS_WALL_PATTERN.search(sample):
            warnings.append("members_only_content_detected")
        if is_likely_auth_wall(text):
            warnings.append("login_required_detected")
    if text and len(text) < MIN_READABLE_SOURCE_CHARS:
        warnings.append("thin_content")
    readable = len(text) >= MIN_READABLE_SOURCE_CHARS and "login_required_detected" not in warnings
    return {"readable": readable, "warnings": warnings}


def url_points_to_pdf(request_url: str) -> bool:
    return (urlparse(request_url).path or "").lower().endswith(".pdf")


def try_pdf_document_fetch(request_url: str) -> dict[str, Any]:
    """URL先のPDFをダウンロードし、Slack添付と同じ経路（Converse document → PyMuPDF）で読む。"""
    record_tool_call("pdf_document_fetch")
    request = urlrequest.Request(
        request_url,
        headers={
            "Accept": "application/pdf, */*;q=0.5",
            "User-Agent": "AI-Shanai-Jirei-Oshiete-Kun/0.1",
        },
    )
    try:
        with urlrequest.urlopen(request, timeout=URL_FETCH_TIMEOUT_SECONDS) as response:
            status_code = int(getattr(response, "status", 200) or 200)
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            data = response.read(MAX_ATTACHMENT_BYTES + 1)
    except urlerror.HTTPError as exc:
        return {"ok": False, "error": "pdf_download_http_error", "status_code": exc.code}
    except Exception as exc:
        log_event("pdf_download_failed", error_type=type(exc).__name__)
        return {"ok": False, "error": "pdf_download_failed"}

    if len(data) > MAX_ATTACHMENT_BYTES:
        return {"ok": False, "status_code": status_code, "error": "pdf_too_large"}
    if content_type != "application/pdf" and not data.startswith(b"%PDF-"):
        # 拡張子は.pdfでも実体がHTML（認証壁・エラーページ等）の場合は通常経路に任せる
        return {"ok": False, "status_code": status_code, "error": "not_pdf_content"}

    name = (urlparse(request_url).path or "").rsplit("/", 1)[-1] or "document.pdf"
    method, text = extract_text_from_binary(data, "pdf", name)
    log_event("pdf_document_fetch_completed", extraction_method=method, chars=len(text))
    if not text:
        return {"ok": False, "status_code": status_code, "error": "pdf_extraction_empty"}
    return {"ok": True, "status_code": status_code, "text": text}


def try_tavily_extract(request_url: str) -> dict[str, Any]:
    if not TAVILY_SEARCH_ENABLED:
        return {"ok": False, "error": "tavily_disabled"}
    try:
        api_key = get_tavily_api_key()
    except Exception as exc:
        log_event("tavily_extract_api_key_unavailable", error_type=type(exc).__name__)
        return {"ok": False, "error": "tavily_api_key_unavailable"}

    record_tool_call("tavily_extract")
    request = urlrequest.Request(
        TAVILY_EXTRACT_ENDPOINT,
        data=json.dumps({"urls": [request_url], "extract_depth": "advanced", "include_images": False}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AI-Shanai-Jirei-Oshiete-Kun/0.1",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(request, timeout=TAVILY_EXTRACT_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        return {"ok": False, "error": "tavily_extract_http_error", "status_code": exc.code}
    except Exception as exc:
        log_event("tavily_extract_failed", error_type=type(exc).__name__)
        return {"ok": False, "error": "tavily_extract_request_failed"}

    results = payload.get("results") if isinstance(payload, dict) else None
    if isinstance(results, list) and results:
        raw_content = str(results[0].get("raw_content") or "")
        if raw_content:
            return {"ok": True, "text": raw_content}
    return {"ok": False, "error": "tavily_extract_empty_result"}


def try_http_request_fetch(request_url: str) -> dict[str, Any]:
    record_tool_call("http_request")
    try:
        result = strands_http_request(
            {
                "toolUseId": f"url_fetch_{uuid.uuid4().hex[:8]}",
                "input": {
                    "method": "GET",
                    "url": request_url,
                    "headers": {
                        "Accept": "text/html, text/plain, application/xhtml+xml, application/json;q=0.8, */*;q=0.4",
                        "Range": "bytes=0-200000",
                        "User-Agent": "AI-Shanai-Jirei-Oshiete-Kun/0.1",
                    },
                    "timeout": URL_FETCH_TIMEOUT_SECONDS,
                    "allow_redirects": True,
                    "max_redirects": 3,
                    "convert_to_markdown": True,
                    "session_config": {
                        "keep_alive": False,
                        "cookie_persistence": False,
                        "max_retries": 0,
                    },
                },
            }
        )
    except Exception as exc:
        log_event("http_request_fetch_failed", error_type=type(exc).__name__)
        return {"ok": False, "error": "request_failed"}

    parsed = parse_strands_http_result(result)
    status_code = parsed.get("status_code")
    body = str(parsed.get("body") or "")
    if parsed.get("tool_status") != "success":
        return {"ok": False, "status_code": status_code, "error": "http_request_tool_error"}
    if not isinstance(status_code, int):
        return {"ok": False, "error": "status_code_missing"}
    if status_code in {401, 403}:
        return {"ok": False, "status_code": status_code, "error": "authentication_required"}
    if not (200 <= status_code < 300):
        return {"ok": False, "status_code": status_code, "error": "non_success_status"}
    if not body:
        return {"ok": False, "status_code": status_code, "error": "empty_body"}
    return {"ok": True, "status_code": status_code, "text": body}


def run_browser_fetch(request_url: str, slide_mode: bool) -> dict[str, Any]:
    """AgentCore Browser でのレンダリング取得をワーカースレッドで実行する。

    Playwright の sync API は実行中の asyncio ループがあるスレッドでは
    使えないため、専用スレッドに逃がして結果だけ受け取る。
    """
    record_tool_call("agentcore_browser")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(browser_fetch_worker, request_url, slide_mode)
        try:
            return future.result(timeout=BROWSER_FETCH_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            log_event("browser_fetch_timeout", url=request_url)
            return {"ok": False, "error": "browser_fetch_timeout"}
        except Exception as exc:
            log_event("browser_fetch_failed", error_type=type(exc).__name__)
            return {"ok": False, "error": "browser_fetch_failed"}


def browser_fetch_worker(request_url: str, slide_mode: bool) -> dict[str, Any]:
    from bedrock_agentcore.tools.browser_client import browser_session
    from playwright.sync_api import sync_playwright

    log_event("browser_fetch_started", url=request_url, slide_mode=slide_mode)
    screenshots: list[bytes] = []
    page_text = ""
    with browser_session(BEDROCK_REGION, identifier=BROWSER_TOOL_IDENTIFIER) as client:
        ws_url, ws_headers = client.generate_ws_headers()
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(ws_url, headers=ws_headers)
            try:
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(15000)
                page.set_viewport_size({"width": 1280, "height": 720})
                page.goto(request_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                page_text = str(page.evaluate("() => document.body ? document.body.innerText : ''") or "")
                if slide_mode or len(page_text) < MIN_READABLE_SOURCE_CHARS:
                    screenshots = capture_slide_screenshots(page, slide_mode)
            finally:
                browser.close()

    if screenshots:
        vision_text = extract_text_from_screenshots(screenshots, request_url)
        if vision_text:
            page_text = f"{vision_text}\n\n{page_text}".strip()
    if not page_text:
        return {"ok": False, "error": "browser_rendered_empty_page"}
    return {"ok": True, "text": page_text}


def capture_slide_screenshots(page: Any, slide_mode: bool) -> list[bytes]:
    # PNGだとBedrockの画像サイズ上限（3.75MB）を超えることがあるためJPEGで撮る
    shots: list[bytes] = []
    try:
        shots.append(page.screenshot(type="jpeg", quality=60, timeout=15000))
        if slide_mode:
            # スライドプレイヤーにフォーカスを移してから矢印キーでページ送りする
            viewport = page.viewport_size or {"width": 1280, "height": 720}
            page.mouse.click(viewport["width"] // 2, viewport["height"] // 2)
            page.wait_for_timeout(500)
            for _ in range(MAX_SLIDE_SCREENSHOTS - 1):
                page.keyboard.press("ArrowRight")
                page.wait_for_timeout(800)
                shot = page.screenshot(type="jpeg", quality=60, timeout=15000)
                if shots and shot == shots[-1]:
                    break
                shots.append(shot)
    except Exception as exc:
        log_event("slide_screenshot_failed", error_type=type(exc).__name__, captured=len(shots))
    return shots


def extract_text_from_screenshots(screenshots: list[bytes], request_url: str) -> str:
    if not screenshots:
        return ""
    content: list[dict[str, Any]] = [
        {
            "text": (
                "以下はWebページまたはスライド資料のスクリーンショットです。"
                "書かれているテキスト内容（タイトル、課題、取り組み、効果、技術要素など）を、"
                "日本語で忠実に書き起こしてください。装飾やレイアウトの説明は不要です。"
                f" 元URL: {request_url}"
            )
        }
    ]
    for shot in screenshots[:MAX_SLIDE_SCREENSHOTS]:
        content.append({"image": {"format": "jpeg", "source": {"bytes": shot}}})
    try:
        response = bedrock_runtime.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": 2000},
        )
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        text = "\n".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict)).strip()
        log_event("screenshot_text_extracted", image_count=len(screenshots), chars=len(text))
        return text
    except Exception as exc:
        log_event("screenshot_text_extraction_failed", error_type=type(exc).__name__)
        return ""


def parse_strands_http_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"tool_status": "error", "body": ""}

    texts: list[str] = []
    for item in result.get("content", []):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            texts.append(item["text"])

    status_code: int | None = None
    body_parts: list[str] = []
    for text in texts:
        if text.startswith("Status Code:"):
            try:
                status_code = int(text.split(":", 1)[1].strip())
            except ValueError:
                status_code = None
        elif text.startswith("Body:"):
            body_parts.append(text.split(":", 1)[1].strip())

    return {
        "tool_status": result.get("status"),
        "status_code": status_code,
        "body": "\n".join(body_parts).strip(),
    }


def normalize_tavily_results(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    normalized: list[dict[str, Any]] = []
    for raw_result in payload.get("results", []):
        if not isinstance(raw_result, dict):
            continue
        url = normalize_external_source_url(str(raw_result.get("url") or ""))
        title = cleanup_generated_text(str(raw_result.get("title") or ""))
        content = str(raw_result.get("content") or "")
        sanitized = sanitize_fetched_source_text(content)
        if not url or not sanitized["text"]:
            continue
        normalized.append(
            {
                "title": truncate(title, 120),
                "url": url,
                "content_excerpt": truncate(sanitized["text"], 900),
                "score": raw_result.get("score"),
                "redaction_status": sanitized["status"],
                "sensitive_flags": sanitized["flags"],
            }
        )
    return normalized[: min(TAVILY_SEARCH_MAX_RESULTS, 3)]


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in URL_PATTERN.finditer(text):
        url = cleanup_url(match.group(0))
        if url and url not in urls:
            urls.append(url)
    return urls


def cleanup_url(url: str) -> str:
    return url.strip("<> \t\r\n").rstrip(".,、。;；:：!?！？】』」})）")


def normalize_fetch_request_url(url: str) -> str:
    parsed = urlparse(cleanup_url(url))
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return cleanup_url(url)

    hostname = parsed.hostname.lower().rstrip(".")
    netloc = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=netloc,
        params="",
        query="",
        fragment="",
    ).geturl()


def normalize_external_source_url(url: str) -> str:
    parsed = urlparse(cleanup_url(url))
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return cleanup_url(url)

    hostname = parsed.hostname.lower().rstrip(".")
    netloc = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return parsed._replace(scheme=parsed.scheme.lower(), netloc=netloc, params="", query="", fragment="").geturl()


def collect_external_source_urls(text: str) -> list[str]:
    return merge_external_source_urls([], [normalize_external_source_url(url) for url in extract_urls(text)])


def normalize_external_source_url_list(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_values = value
    elif isinstance(value, str):
        raw_values = re.split(r"[\s,、\n]+", value)
    else:
        raw_values = []
    return merge_external_source_urls([], [str(item) for item in raw_values if str(item).strip()])


def current_payload_source_urls() -> list[str]:
    payload = _current_payload.get()
    values: list[str] = [str(payload.get("text", ""))]
    thread_messages = payload.get("thread_messages", [])
    if isinstance(thread_messages, list):
        for message in thread_messages:
            if isinstance(message, dict):
                values.append(str(message.get("text", "")))
    return collect_external_source_urls("\n".join(values))


def current_web_source_urls() -> list[str]:
    return merge_external_source_urls([], _web_source_urls.get())


def has_readable_fetched_url_sources() -> bool:
    return any(bool(source.get("ok") and source.get("content_excerpt")) for source in _fetched_url_sources.get())


URL_FETCH_METHOD_LABELS = {
    "tavily_extract": "Tavilyの本文抽出",
    "http_request": "HTTPリクエスト",
    "agentcore_browser": "ブラウザでのレンダリング",
}

URL_FETCH_ERROR_LABELS = {
    "authentication_required": "ログインが必要なページでした",
    "login_page_or_unreadable_content": "ログインページが表示され本文を読めませんでした",
    "non_success_status": "ページがエラーを返しました",
    "dns_lookup_failed": "ホスト名を解決できませんでした",
    "blocked_host": "社内ポリシーで取得対象外のホストでした",
    "blocked_ip_address": "社内ポリシーで取得対象外のアドレスでした",
    "resolved_to_blocked_ip_address": "社内ポリシーで取得対象外のアドレスでした",
    "browser_fetch_timeout": "ブラウザでの読み込みが時間内に終わりませんでした",
    "empty_body": "ページから本文テキストを取得できませんでした",
    "unreadable_content": "本文テキストを取得できませんでした",
    "drive_file_not_accessible": "Botのアカウントから資料を開けませんでした",
    "drive_credentials_unavailable": "Bot側のDrive認証でエラーが起きました",
    "drive_metadata_fetch_failed": "Bot側でDrive資料の情報を取得できませんでした",
    "drive_download_failed": "Bot側でDrive資料の本文を取得できませんでした",
}


def describe_url_fetch_issue(source: dict[str, Any]) -> str:
    warnings = source.get("warnings") or []
    if "members_only_content_detected" in warnings:
        return "会員限定コンテンツの可能性があります"
    if "login_required_detected" in warnings:
        return "ログインが必要なページのため本文を読めませんでした"
    error = str(source.get("error") or "")
    if error in URL_FETCH_ERROR_LABELS:
        return URL_FETCH_ERROR_LABELS[error]
    if error:
        return "本文を取得できませんでした"
    if "thin_content" in warnings:
        return "取得できたテキストがわずかでした"
    return ""


def format_url_fetch_notice_lines() -> list[str]:
    lines: list[str] = []
    for source in _fetched_url_sources.get():
        url = source.get("url")
        if not url:
            continue
        issue = describe_url_fetch_issue(source)
        if not issue:
            continue
        if source.get("ok"):
            lines.append(f"※ {url} は{issue}。読み取れた範囲だけ参考にしています。")
        else:
            lines.append(f"※ {url} は{issue}。本文の要点をこのスレッドで教えてもらえれば追記します。")
    return lines


def compact_url_fetch_results() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for source in _fetched_url_sources.get()[:MAX_FETCH_URLS]:
        item: dict[str, Any] = {"url": source.get("url"), "ok": bool(source.get("ok"))}
        if source.get("method"):
            item["method"] = source["method"]
        if source.get("error"):
            item["error"] = source["error"]
        if source.get("warnings"):
            item["warnings"] = source["warnings"]
        attempts = source.get("attempts") or []
        if attempts:
            item["attempts"] = [
                {key: attempt.get(key) for key in ("method", "ok", "error", "status_code") if attempt.get(key) is not None}
                for attempt in attempts
            ]
        results.append(item)
    return results


def merge_external_source_urls(current: Any, incoming: list[str]) -> list[str]:
    current_urls = current if isinstance(current, list) else []
    urls = [normalize_external_source_url(str(url)) for url in [*current_urls, *incoming] if str(url).strip()]
    return list(dict.fromkeys(urls))[:10]


def validate_fetch_url(url: str) -> tuple[bool, str]:
    if len(url) > 2048:
        return False, "url_too_long"

    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return False, "unsupported_scheme"
    if parsed.username or parsed.password:
        return False, "userinfo_not_allowed"
    if not parsed.hostname:
        return False, "hostname_missing"

    host = parsed.hostname.lower().rstrip(".")
    if is_blocked_fetch_host(host):
        return False, "blocked_host"
    if is_blocked_ip_literal(host):
        return False, "blocked_ip_address"

    try:
        resolved = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False, "dns_lookup_failed"

    for item in resolved:
        ip_text = item[4][0]
        if is_blocked_ip_literal(ip_text):
            return False, "resolved_to_blocked_ip_address"
    return True, "ok"


def is_blocked_fetch_host(host: str) -> bool:
    if host in BLOCKED_FETCH_HOSTS:
        return True
    return host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal")


def is_blocked_ip_literal(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return False
    return any(
        [
            ip.is_loopback,
            ip.is_private,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        ]
    )


def sanitize_fetched_source_text(text: str) -> dict[str, Any]:
    flags: list[str] = []

    def replace(pattern: re.Pattern[str], replacement: str, flag: str, value: str) -> str:
        def mark(_match: re.Match[str]) -> str:
            flags.append(flag)
            return replacement

        return pattern.sub(mark, value)

    sanitized = text
    sanitized = replace(EMAIL_PATTERN, "[メールアドレス省略]", "email", sanitized)
    sanitized = replace(PHONE_PATTERN, "[電話番号省略]", "phone", sanitized)
    sanitized = replace(CUSTOMER_CONTACT_PATTERN, "[顧客担当者名省略]", "customer_contact", sanitized)
    sanitized = replace(TOKEN_PATTERN, "[トークン省略]", "secret_or_token", sanitized)
    sanitized = replace(GOOGLE_API_KEY_PATTERN, "[APIキー省略]", "secret_or_token", sanitized)
    sanitized = replace(TAVILY_API_KEY_PATTERN, "[APIキー省略]", "secret_or_token", sanitized)
    sanitized = replace(PASSWORD_HINT_PATTERN, "[秘密情報省略]", "secret_or_token", sanitized)
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    sanitized = re.sub(r"[ \t]{2,}", " ", sanitized).strip()

    unique_flags = sorted(set(flags))
    return {"status": "masked" if unique_flags else "safe", "text": sanitized, "flags": unique_flags}


def is_likely_auth_wall(text: str) -> bool:
    sample = text[:3000].lower()
    login_markers = [
        "sign in",
        "sign-in",
        "log in",
        "login",
        "ログイン",
        "認証",
        "sso",
        "single sign-on",
        "google アカウント",
        "atlassian account",
    ]
    password_markers = ["password", "パスワード", "メールアドレス", "email address"]
    return any(marker in sample for marker in login_markers) and any(marker in sample for marker in password_markers)


async def run_agent(prompt: str) -> str:
    text_chunks: list[str] = []
    final_result: Any = None
    async for event in get_agent().stream_async(prompt):
        if "current_tool_use" in event:
            tool_info = event.get("current_tool_use") or {}
            if isinstance(tool_info, dict):
                tool_name = str(tool_info.get("name") or "unknown")
                record_tool_call(tool_name)
                log_event("strands_tool_use", tool=tool_name)
        elif "data" in event:
            text_chunks.append(str(event["data"]))
        elif "result" in event:
            final_result = event["result"]

    if text_chunks:
        return "".join(text_chunks)
    if final_result is not None:
        return result_to_text(final_result)
    return ""


def record_tool_call(tool_name: str) -> None:
    calls = _tool_calls.get()
    if not calls or calls[-1] != tool_name:
        calls.append(tool_name)


def remember_tool_result(result: dict[str, Any], tool_name: str) -> str:
    record_tool_call(tool_name)
    result_with_metadata = with_agent_metadata(result)
    _tool_results.get().append(result_with_metadata)
    log_event(
        "strands_tool_result",
        tool=tool_name,
        action=result.get("action"),
        ok=result.get("ok"),
        case_id=result.get("case_id"),
        deleted_count=result.get("deleted_count"),
    )
    return json.dumps(result_with_metadata, ensure_ascii=False, default=json_fallback)


def tool_result_tracking_error_reply() -> dict[str, Any]:
    return {
        "ok": False,
        "action": "error",
        "slack_text": (
            "処理ツールは動きましたが、結果の確認に失敗しました。"
            "二重登録を避けるため、すぐには再送せず、このスレッドで管理者の確認をお待ちください。"
        ),
        "error": "tool_result_tracking_failed",
    }


def agent_routing_error_reply() -> dict[str, Any]:
    return {
        "ok": False,
        "action": "error",
        "slack_text": (
            "依頼内容に合う処理を確定できませんでした。"
            "登録・更新・検索は実行していません。少し待ってから、もう一度メンションしてください。"
        ),
        "error": "business_tool_not_selected",
    }


def append_graphic_notice_if_needed(result: dict[str, Any]) -> dict[str, Any]:
    """登録・更新後に chat_reply_tool のまとめ返信で終わった場合でも、定型のグラレコ案内を必ず出す。

    register/update ツール自身の slack_text には案内が含まれるが、複数事例の
    まとめ報告など最終応答が別ツールになると案内が落ちるため、コードで補完する。
    """
    if not isinstance(result, dict) or not any(_graphic_pending_scheduled.get()):
        return result
    slack_text = str(result.get("slack_text") or "")
    if not slack_text or GRAPHIC_PENDING_NOTICE in slack_text:
        return result
    if result.get("ok") is False:
        return result
    return {**result, "slack_text": f"{slack_text}\n\n{GRAPHIC_PENDING_NOTICE}"}


def with_agent_metadata(result: dict[str, Any]) -> dict[str, Any]:
    posting_guard = {"post_to_channel": False} if _current_payload.get().get("post_to_channel") is False else {}
    return {
        **result,
        **posting_guard,
        "agent": {
            "runtime": "agentcore",
            "framework": "strands",
            "model_id": BEDROCK_MODEL_ID,
            "tool_calls": list(_tool_calls.get()),
        },
    }


def log_event(message: str, **fields: Any) -> None:
    logger.info(
        json.dumps(
            {
                "level": "info",
                "message": message,
                **{key: value for key, value in fields.items() if value is not None},
            },
            ensure_ascii=False,
        )
    )


def normalize_slack_text(text: str) -> str:
    text = re.sub(r"<@[^>]+>", " ", text)
    text = re.sub(r"&lt;@[^&]+&gt;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tool_content_with_payload(tool_argument: str) -> str:
    argument = normalize_slack_text(tool_argument)
    payload_text = normalize_slack_text(str(_current_payload.get().get("text", "")))
    if not payload_text:
        return argument
    if not argument:
        return payload_text
    if argument == payload_text:
        return argument
    if argument in payload_text or looks_like_update_request(payload_text):
        if should_preserve_generated_update_content(argument, payload_text):
            return argument
        return payload_text
    return argument


def should_preserve_generated_update_content(argument: str, payload_text: str) -> bool:
    if not argument or argument == payload_text or argument in payload_text:
        return False
    if not looks_like_update_request(payload_text):
        return False
    if len(argument) >= 80:
        return True
    if has_readable_fetched_url_sources() and len(argument) >= 24:
        return True
    return False


def looks_like_update_request(text: str) -> bool:
    return bool(re.search(r"(更新|追記|補足|ブラッシュアップ)", normalize_slack_text(text)))


def sanitize_slack_text(text: str) -> dict[str, Any]:
    flags: list[str] = []

    def replace(pattern: re.Pattern[str], replacement: str, flag: str, value: str) -> str:
        def mark(_match: re.Match[str]) -> str:
            flags.append(flag)
            return replacement

        return pattern.sub(mark, value)

    sanitized = text
    sanitized = replace(EMAIL_PATTERN, "[メールアドレス省略]", "email", sanitized)
    sanitized = replace(PHONE_PATTERN, "[電話番号省略]", "phone", sanitized)
    sanitized = replace(CUSTOMER_CONTACT_PATTERN, "[顧客担当者名省略]", "customer_contact", sanitized)

    if TOKEN_PATTERN.search(sanitized) or GOOGLE_API_KEY_PATTERN.search(sanitized) or TAVILY_API_KEY_PATTERN.search(sanitized) or PASSWORD_HINT_PATTERN.search(sanitized):
        return {"status": "blocked", "text": "[投稿禁止情報を検出しました]", "flags": sorted(set(flags + ["secret_or_token"]))}

    unique_flags = sorted(set(flags))
    return {"status": "masked" if unique_flags else "safe", "text": sanitized, "flags": unique_flags}


def sanitize_error_message(exc: Exception) -> str:
    message = str(exc)
    message = EMAIL_PATTERN.sub("[メールアドレス省略]", message)
    message = PHONE_PATTERN.sub("[電話番号省略]", message)
    message = TOKEN_PATTERN.sub("[トークン省略]", message)
    message = GOOGLE_API_KEY_PATTERN.sub("[APIキー省略]", message)
    message = TAVILY_API_KEY_PATTERN.sub("[APIキー省略]", message)
    message = PASSWORD_HINT_PATTERN.sub("[秘密情報省略]", message)
    return truncate(re.sub(r"\s+", " ", message).strip(), 300)


def normalize_case_card_fields(content: str, fields: dict[str, Any], fill_defaults: bool = True) -> dict[str, Any]:
    normalized: dict[str, Any] = {"blocked": False, "sensitive_flags": []}
    flags: list[str] = []
    text_fields = [
        "title",
        "summary",
        "project_or_customer",
        "customer_industry",
        "department",
        "contact_hint",
        "problem",
        "approach",
        "impact",
    ]
    for key in text_fields:
        value = cleanup_generated_text(str(fields.get(key) or ""))
        if not value:
            continue
        redaction = sanitize_slack_text(value)
        if redaction["status"] == "blocked":
            normalized["blocked"] = True
            return normalized
        flags.extend(redaction["flags"])
        normalized[key] = redaction["text"]

    for key in ["technologies", "tags"]:
        values = normalize_string_list(fields.get(key))
        if key == "tags":
            values = normalize_tag_list(values)
        safe_values: list[str] = []
        for value in values:
            redaction = sanitize_slack_text(value)
            if redaction["status"] == "blocked":
                normalized["blocked"] = True
                return normalized
            flags.extend(redaction["flags"])
            if redaction["text"]:
                safe_values.append(cleanup_generated_text(redaction["text"]))
        if safe_values:
            normalized[key] = list(dict.fromkeys(safe_values))[:12]

    if fill_defaults and not normalized.get("title"):
        normalized["title"] = build_case_title(content)
    if fill_defaults and not normalized.get("summary"):
        normalized["summary"] = build_summary(content)

    normalized["sensitive_flags"] = sorted(set(flags))
    return normalized


def normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_values = value
    elif isinstance(value, str):
        raw_values = re.split(r"[,、/・\n]+", value)
    else:
        raw_values = []
    return [
        cleanup_generated_text(str(item))
        for item in raw_values
        if cleanup_generated_text(str(item))
    ]


def normalize_tag_list(values: list[str]) -> list[str]:
    tags: list[str] = []
    for value in values:
        tag = cleanup_generated_text(value)
        tag = re.sub(r"^(?:顧客|対象|業界|用途|技術|部署|チーム)\s*[:：]\s*", "", tag)
        tag = re.sub(r"(?:向け|向けに)$", "", tag).strip(" 、。")
        if is_search_tag_like(tag):
            tags.append(tag)
    return list(dict.fromkeys(tags))


def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def is_search_tag_like(tag: str) -> bool:
    if not tag or len(tag) > 28:
        return False
    if any(marker in tag for marker in ["架空", "テスト用", "営業向け", "紹介できます"]):
        return False
    if re.search(r"[。！？?!]", tag):
        return False
    if re.search(r"(?:です|ます|ました|ください|できます|しました|実施)$", tag):
        return False
    if re.search(r"(?:向けに|向けには|狙った|として|組み合わせた)", tag):
        return False
    return True


def cleanup_generated_text(text: str) -> str:
    text = display_content(text)
    text = re.sub(r"[（(]\s*[）)]", "", text)
    text = re.sub(r"^[-・\s]+", "", text)
    text = re.sub(r"(?:を)?(?:事例登録|登録|追加|保存)(?:して|しといて|お願いします|してください|して下さい)?[。.]?$", "", text)
    text = re.sub(r"^(?:事例登録|登録|追加|保存)\s*[:：、。-]?\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip(" 、。:：")
    return text


def determine_case_status(title: str, summary: str, attributes: dict[str, Any]) -> str:
    if len(summary) < 24:
        return "draft"
    if not title:
        return "draft"
    if not any(attributes.get(key) for key in ["project_or_customer", "approach", "problem", "customer_industry", "tags"]):
        return "draft"
    return "ready"


def build_follow_up_questions(record: dict[str, Any], suggested: list[str]) -> list[str]:
    questions: list[str] = []
    for question in suggested:
        clean = cleanup_generated_text(question)
        if clean:
            questions.append(clean)

    if not record.get("impact"):
        questions.append("成果や効果が分かれば、短縮時間・品質向上・反応などを一言で教えてください。")
    if not record.get("department") and not record.get("contact_hint"):
        questions.append("担当部署や聞きに行ける社内担当者が分かれば教えてください。")
    if not record.get("customer_industry"):
        questions.append("顧客の業界や用途カテゴリが分かれば教えてください。")

    return list(dict.fromkeys(questions))[:MAX_FOLLOW_UP_QUESTIONS]


def build_keywords(content: str, attributes: dict[str, Any]) -> list[str]:
    values: list[str] = []
    values.extend(extract_keywords(content))
    for key in [
        "project_or_customer",
        "customer_industry",
        "department",
        "contact_hint",
        "problem",
        "approach",
        "impact",
        "case_type",
        "team",
    ]:
        value = attributes.get(key)
        if isinstance(value, str):
            values.extend(extract_keywords(value))
    for key in ["technologies", "tags"]:
        value = attributes.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value if str(item).strip())
    values.extend(infer_keywords_from_text(" ".join(values + [content])))
    return list(dict.fromkeys(cleanup_generated_text(value) for value in values if cleanup_generated_text(value)))[:30]


def infer_keywords_from_text(text: str) -> list[str]:
    normalized = normalize_for_search(text)
    keywords: list[str] = []
    if any(token in normalized for token in ["エネルギー", "電力", "電力計", "energy"]):
        keywords.extend(["エネルギー", "電力"])
    if any(token in normalized for token in ["aiエージェント", "agent", "エージェント", "マルチエージェント", "agentcore", "strands"]):
        keywords.extend(["AIエージェント", "エージェント"])
    if any(token in normalized for token in ["faq", "ナレッジ", "問い合わせ", "問合せ"]):
        keywords.extend(["FAQ", "ナレッジ検索", "問い合わせ対応"])
    if any(token in normalized for token in ["コンサル", "支援", "レビュー"]):
        keywords.append("コンサル")
    return keywords


def format_slack_sections(*sections: Any) -> str:
    blocks: list[str] = []
    for section in sections:
        if not section:
            continue
        if isinstance(section, str):
            lines = section.splitlines()
        else:
            try:
                lines = [str(line) for line in section]
            except TypeError:
                lines = [str(section)]
        cleaned = [line.strip() for line in lines if str(line).strip()]
        if cleaned:
            blocks.append("\n".join(cleaned))
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(blocks)).strip()


def format_usage_reply(prefix: str | None = None) -> str:
    return format_slack_sections(
        [prefix] if prefix else [],
        [
            "このアプリは現在、社内の案件情報・AI活用事例の概要登録と検索に対応しています。",
            "登録済みの事例への追記や、直前に検索した事例の詳細確認もできます。",
        ],
        [
            "使い方の例",
            "・登録: 「この取り組みを事例として登録して。〇〇案件で、□□チームが△△をしました」",
            "・検索: 「議事録AIっぽい事例ある？」「エネルギー業界向けのAI活用を教えて」",
            "・追記: 登録済み事例のスレッドで、Bot にメンションして追加情報を書いてください。",
        ],
        [
            "メールアドレス、電話番号、顧客担当者名、APIキーなどは投稿・保存しないように扱います。",
        ],
    )


def is_bot_design_question(text: str, thread_messages: Any = None) -> bool:
    normalized = normalize_slack_text(text)
    if BOT_CASE_TECHNOLOGY_QUESTION_PATTERN.search(normalized):
        return False
    # 「グラレコできた？」は画像そのものの要求であって、生成方式の設計質問ではない。
    if is_case_graphic_request(normalized):
        return False
    if is_explicit_bot_design_question(normalized):
        return True
    prior_question = find_prior_bot_design_question(thread_messages)
    if BOT_REANSWER_REQUEST_PATTERN.search(normalized):
        return bool(prior_question)
    if not BOT_DESIGN_TOPIC_PATTERN.search(normalized):
        return False
    return bool(prior_question)


def is_explicit_bot_design_question(text: str) -> bool:
    normalized = normalize_slack_text(text)
    if BOT_CASE_TECHNOLOGY_QUESTION_PATTERN.search(normalized):
        return False
    return bool(BOT_SELF_REFERENCE_PATTERN.search(normalized) and BOT_DESIGN_TOPIC_PATTERN.search(normalized))


def find_prior_bot_design_question(thread_messages: Any) -> str:
    if not isinstance(thread_messages, list):
        return ""
    for message in reversed(thread_messages):
        if not isinstance(message, dict) or message.get("bot_id") or message.get("is_current"):
            continue
        prior_text = normalize_slack_text(str(message.get("text", "")))
        if is_explicit_bot_design_question(prior_text):
            return prior_text
    return ""


def resolve_bot_design_question(payload: dict[str, Any], provided_question: str = "") -> str:
    current_question = normalize_slack_text(str(payload.get("text", "")))
    provided = normalize_slack_text(provided_question)
    thread_messages = payload.get("thread_messages")

    if is_explicit_bot_design_question(current_question):
        return current_question
    if BOT_REANSWER_REQUEST_PATTERN.search(current_question):
        prior_question = find_prior_bot_design_question(thread_messages)
        if prior_question:
            return prior_question
    # 「会話側のモデルは？」のような短い追質問は、元の設計質問が同じスレッドに
    # ある場合だけ現在文を採用する。
    if BOT_DESIGN_TOPIC_PATTERN.search(current_question) and find_prior_bot_design_question(thread_messages):
        return current_question
    if is_explicit_bot_design_question(provided):
        return provided
    return current_question or provided


def resolve_bot_design_addressee(payload: dict[str, Any]) -> str:
    current_question = normalize_slack_text(str(payload.get("text", "")))
    if not BOT_REANSWER_REQUEST_PATTERN.search(current_question):
        return ""

    candidate_texts = [current_question]
    thread_messages = payload.get("thread_messages")
    if isinstance(thread_messages, list):
        for message in reversed(thread_messages):
            if not isinstance(message, dict) or message.get("bot_id") or message.get("is_current"):
                continue
            candidate_texts.append(normalize_slack_text(str(message.get("text", ""))))

    for text in candidate_texts:
        match = BOT_QUESTION_ADDRESSEE_PATTERN.search(text)
        if match:
            addressee = cleanup_generated_text(match.group(1))
            addressee = re.sub(
                r"^(?:改めて|もう一度|もう1度|もっかい|再度|では|じゃあ|ごめん)+",
                "",
                addressee,
            )
            if addressee:
                return addressee
    return ""


def bot_design_topics(question: str) -> list[str]:
    normalized = normalize_slack_text(question)
    topics: list[str] = []
    patterns = [
        ("graphic", r"グラレコ|画像生成|文字化け|フォント"),
        ("model", r"モデル|LLM|Claude|Gemini|Bedrock"),
        ("prompt", r"プロンプト"),
        ("architecture", r"設計|仕組み|構成|アーキテクチャ|技術スタック|実装|どう作|どう動|何で動|AgentCore|Strands"),
        ("search", r"検索方式|検索.{0,8}(?:仕組み|設計)|RAG|ナレッジベース"),
        ("storage", r"データ保存|どこに保存"),
        ("security", r"セキュリティ|情報保護|個人情報|マスク|秘密情報"),
        ("schedule", r"日次共有|定期共有|スケジュール"),
    ]
    for topic, pattern in patterns:
        if re.search(pattern, normalized, re.IGNORECASE):
            topics.append(topic)
    return topics


def format_bot_design_reply(question: str, addressee: str = "") -> str:
    topics = bot_design_topics(question)
    selected = set(topics)
    if not selected:
        return (
            "もちろんです。このBotのモデル、グラレコ生成、検索など、"
            "気になっている部分をもう少し具体的に教えてもらえれば、その点に絞って答えます。"
        )

    sections: list[str] = []
    if "graphic" in selected:
        if addressee:
            sections.append(f"{addressee}、ご質問ありがとうございます！")
        if not GRAPHIC_GENERATION_ENABLED:
            sections.append(
                "グラレコ画像の生成は任意機能で、この環境では無効です。"
                "利用する場合は、デプロイ設定で画像生成を有効にし、対応するモデルと認証情報を設定します。"
            )
        else:
            sections.append(
                "はい、文字化けを減らすために、画像モデルとプロンプトの両方で工夫しています。"
            )
            sections.append(
                f"グラレコには、Google Cloud の Vertex AI で `{GEMINI_IMAGE_MODEL}` を使っています。"
                f"画像は{GEMINI_IMAGE_SIZE}・16:9で生成しています。"
            )
            sections.append(
                "プロンプトでは、次のようにかなり具体的に指示しています。\n"
                "・文字は正確で読みやすい日本語にする\n"
                "・長文をそのまま書かず、短い見出しとキーフレーズに分ける\n"
                "・内容を5〜7個の箱に整理する\n"
                "・事例にない数値や固有名詞は作らない"
            )
            sections.append(
                "生成後にOCRで文字を修正しているわけではなく、モデルの描画性能に加えて、"
                "画像内の文章を短く構造化することで文字化けしにくくしています。"
                "完全にゼロとまでは保証できませんが、今のところこの組み合わせがうまく機能しています。"
            )

    if "model" in selected or ("graphic" in selected and "モデル" in normalize_slack_text(question)):
        if "graphic" in selected:
            sections.append(
                f"ちなみに、Botの会話や事例整理には Amazon Bedrock の `{BEDROCK_MODEL_DISPLAY_NAME}` を使っています。"
                "こちらはグラレコ画像を作るモデルとは別です。"
            )
        else:
            if addressee:
                sections.append(f"{addressee}、ご質問ありがとうございます！")
            sections.append(
                f"Botの会話理解・事例整理・ツール選択には、Amazon Bedrock の "
                f"`{BEDROCK_MODEL_DISPLAY_NAME}` を使っています。"
            )
            if GRAPHIC_GENERATION_ENABLED:
                sections.append(f"グラレコ画像は別で、Vertex AI の `{GEMINI_IMAGE_MODEL}` です。")

    if "prompt" in selected and "graphic" not in selected:
        sections.append(
            "会話側の指示では、登録・更新・検索・詳細確認・担当者への橋渡しを使い分け、"
            "登録情報にない内容は推測せず、社内担当者には「さん」を付け、"
            "秘密情報を投稿・保存しないことを明示しています。"
            "内部のシステムプロンプト全文やリソース識別子、秘密値はSlackには出しませんが、設計意図や工夫は説明できます。"
        )

    if "architecture" in selected:
        sections.append(
            "全体は、Slackのメンションを API Gateway と Lambda で署名検証してすぐ受け付け、"
            "SQSの待ち行列から Amazon Bedrock AgentCore Runtime 上の Strands Agent が本処理を行う構成です。"
            "LambdaはSlackとの受け渡し、Agent側は検索・登録・更新・回答を担当するよう役割を分けています。"
        )

    if "search" in selected:
        sections.append(
            "検索は、DynamoDBの登録データを正本にし、S3のMarkdownを Amazon Bedrock Knowledge Bases と"
            " S3 Vectorsで意味検索します。キーワード検索も併用し、意味が近いだけの無関係な事例を出しにくくしています。"
        )

    if "storage" in selected:
        asset_description = "検索用Markdownとグラレコ画像" if GRAPHIC_GENERATION_ENABLED else "検索用Markdown"
        sections.append(
            f"事例の構造化データはDynamoDB、{asset_description}はS3に保存しています。"
            "Slackのスレッド状態もDynamoDBで持ち、追質問を直前の事例につなげています。"
        )

    if "security" in selected:
        sections.append(
            "メールアドレス、電話番号、顧客担当者名、APIキー、パスワード、トークンは、"
            "Slackへ返す前と保存前にマスクまたはブロックします。"
            "秘密値はコードに置かず、AWS Systems Manager Parameter Store の暗号化領域で管理しています。"
        )

    if "schedule" in selected:
        scheduled_jobs = "日次共有と検索用ナレッジの同期"
        if GRAPHIC_GENERATION_ENABLED:
            scheduled_jobs = "日次共有、保留中グラレコの定期生成、検索用ナレッジの同期"
        sections.append(f"{scheduled_jobs}は、EventBridge Schedulerから起動しています。")
        if GRAPHIC_GENERATION_ENABLED:
            sections.append("グラレコは追加情報を待つため、登録・更新の5分後から生成します。")

    return format_slack_sections(*sections)


def is_help_request(text: str) -> bool:
    return bool(HELP_REQUEST_PATTERN.search(normalize_slack_text(text)))


def looks_like_out_of_scope_request(text: str) -> bool:
    normalized = normalize_slack_text(text)
    if is_help_request(normalized):
        return False
    if (
        OUT_OF_SCOPE_REQUEST_PATTERN.search(normalized)
        and not CASE_NEXT_ACTION_PATTERN.search(normalized)
        and not re.search(r"(事例|案件|取り組み|取組|活用)", normalized)
    ):
        return True
    if re.search(r"(事例|案件|取り組み|取組|AI|生成AI|エージェント|bot|ボット|Rovo|Copilot|Claude|Gemini)", normalized, re.IGNORECASE):
        return False
    return False


def format_case_context(record: dict[str, Any]) -> str:
    parts: list[str] = []
    if record.get("customer_industry"):
        parts.append(f"業界: {record['customer_industry']}")
    responsible = format_responsible_value(record)
    if responsible:
        parts.append(f"担当: {responsible}")
    return " / ".join(parts)


def format_daily_case_intro(record: dict[str, Any]) -> str:
    industry = format_industry_intro(record.get("customer_industry"))
    title = format_case_title_for_sentence(record.get("title", "社内AI活用"))
    owner_prefix = format_responsible_intro_prefix(record, title)
    return f"{industry}の事例紹介！{owner_prefix}{title}です:tada:"


def format_industry_intro(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "社内AI活用"
    primary = cleanup_generated_text(re.split(r"\s*/\s*", value, maxsplit=1)[0])
    if "飲料" in primary:
        return "飲料業界"
    if "食品" in primary:
        return "食品業界"
    if "社内" in primary:
        return "社内AI活用"
    if "エネルギー" in primary or "電力" in primary:
        return "エネルギー業界"
    if primary.endswith(("業界", "領域", "向け")):
        return primary
    return f"{primary}業界"


def format_case_title_for_sentence(value: Any) -> str:
    title = cleanup_generated_text(str(value or "社内AI活用"))
    title = re.sub(r"(?:の)?(?:事例|取り組み)$", "", title).strip(" 。、")
    return f"{title}事例"


def format_responsible_intro_prefix(record: dict[str, Any], title: str) -> str:
    owner_name = format_internal_people_with_honorifics(record.get("owner_name"))
    parts = compact_responsible_parts([
        format_internal_people_with_honorifics(record.get("contact_hint")),
        " ".join(part for part in [record.get("owner_department"), owner_name] if isinstance(part, str) and part),
        record.get("department"),
        record.get("team"),
    ])
    if not parts:
        return ""

    normalized_title = normalize_for_search(title)
    if len(parts) == 1:
        first = parts[0]
        if normalize_for_search(first) in normalized_title:
            return ""
        return f"{first}の"

    lead = parts[0]
    secondary = next((part for part in parts[1:] if normalize_for_search(part) not in normalized_title), "")
    if secondary:
        return f"{lead}たちの{secondary}の"
    return f"{lead}たちの"


def format_case_paragraph(record: dict[str, Any], max_length: int | None = None) -> str:
    text = join_case_sentences([record.get("summary")])
    if not text:
        text = "社内でAI活用を進めている取り組みです"
    paragraph = make_natural_sentence(text)
    return shorten_slack_paragraph(paragraph, max_length) if max_length else paragraph


def format_daily_case_paragraph(record: dict[str, Any], max_length: int | None = None) -> str:
    text = join_case_sentences([record.get("summary")])
    if not text:
        text = "社内でAI活用を進めている取り組みです"
    paragraph = make_lively_sentence(text)
    return shorten_slack_paragraph(paragraph, max_length) if max_length else paragraph


def join_case_sentences(values: list[Any]) -> str:
    output: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = cleanup_generated_text(value)
        if not cleaned:
            continue
        normalized = normalize_for_search(cleaned)
        if any(normalized and normalized in normalize_for_search(existing) for existing in output):
            continue
        output.append(cleaned)
    return " ".join(output)


def make_lively_sentence(text: str) -> str:
    cleaned = polish_slack_paragraph_style(cleanup_generated_text(text).strip())
    if not cleaned:
        return ""
    if cleaned.endswith(("！", "!", "？", "?")):
        return cleaned
    if cleaned.endswith("。"):
        stem = cleaned[:-1]
        if stem.endswith(("です", "ます", "でした", "ました", "している", "しています", "できる", "できています")):
            return f"{stem}！"
        return f"{stem}です！"
    return f"{cleaned}！"


def make_natural_sentence(text: str) -> str:
    cleaned = polish_slack_paragraph_style(cleanup_generated_text(text).strip())
    if not cleaned:
        return ""
    cleaned = re.sub(r"[！!]+$", "。", cleaned)
    if cleaned.endswith(("。", "？", "?")):
        return cleaned
    return f"{cleaned}。"


def shorten_slack_paragraph(text: str, max_length: int) -> str:
    """文字数は「完結した文をいくつ表示するか」の目安として使う。

    文の途中を切って「…」を付けると内容も文体も不自然になるため、
    目安を超える場合は収まる完結文だけを返す。先頭の1文自体が長い場合は、
    日本語や意味を壊してまで加工せず、その完結文をそのまま返す。
    """
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= max_length:
        return cleaned

    sentences = [match.group(0).strip() for match in re.finditer(r"[^。！？!?]+[。！？!?]", cleaned)]
    if not sentences:
        return make_natural_sentence(cleaned.rstrip("…‥."))

    selected: list[str] = []
    selected_length = 0
    for sentence in sentences:
        if selected and selected_length + len(sentence) > max_length:
            break
        if not selected and len(sentence) > max_length:
            return sentence
        selected.append(sentence)
        selected_length += len(sentence)
    return "".join(selected) if selected else sentences[0]


def polish_slack_paragraph_style(text: str) -> str:
    replacements = [
        ("試している。", "試しています。"),
        ("行っている。", "行っています。"),
        ("実施している。", "実施しています。"),
        ("支援している。", "支援しています。"),
        ("活用している。", "活用しています。"),
        ("構想。", "構想です。"),
    ]
    polished = text
    for before, after in replacements:
        polished = polished.replace(before, after)
    polished = re.sub(r"構想$", "構想です", polished)
    return polished


def format_title_list(records: list[dict[str, Any]]) -> str:
    titles = [f"「{record['title']}」" for record in records if record.get("title")]
    return "、".join(titles)


def format_inline_questions(questions: list[str]) -> str:
    cleaned = [cleanup_generated_text(question).rstrip("？?。") for question in questions if cleanup_generated_text(question)]
    if not cleaned:
        return "補足情報"
    return "、".join(cleaned[:MAX_FOLLOW_UP_QUESTIONS])


def format_responsible_value(values: dict[str, Any]) -> str:
    owner_name = format_internal_people_with_honorifics(values.get("owner_name"))
    owner = " ".join(part for part in [values.get("owner_department"), owner_name] if isinstance(part, str) and part)
    return " / ".join(compact_responsible_parts([
        format_internal_people_with_honorifics(values.get("contact_hint")),
        owner,
        values.get("department"),
        values.get("team"),
    ]))


INTERNAL_PERSON_HONORIFICS = ("さん", "氏", "様", "君", "くん", "ちゃん")
INTERNAL_ORGANIZATION_TERMS = (
    "部",
    "室",
    "課",
    "本部",
    "チーム",
    "グループ",
    "センター",
    "プロジェクト",
    "委員会",
    "会社",
    "担当",
    "メンバー",
)


def format_internal_people_with_honorifics(value: Any) -> str:
    """社内担当者の表示名らしい要素へ、一人ずつ敬称を補う。

    contact_hint には部署名やチーム名も入り得るため、日本人名らしい短い表記だけを
    対象にする。既に敬称がある場合と、組織名らしい表記は変更しない。
    """
    if not isinstance(value, str):
        return ""
    cleaned = cleanup_generated_text(value)
    if not cleaned:
        return ""
    return "".join(
        part if re.fullmatch(r"\s*[、,，/／]\s*", part) else add_internal_person_honorific(part)
        for part in re.split(r"(\s*[、,，/／]\s*)", cleaned)
    )


def add_internal_person_honorific(value: str) -> str:
    token = value.strip()
    if not token:
        return token

    context_match = re.fullmatch(r"(?P<name>.+?)(?P<context>[（(][^）)]*[）)])?", token)
    if not context_match:
        return token
    name = context_match.group("name").strip()
    context = context_match.group("context") or ""
    if name.endswith(INTERNAL_PERSON_HONORIFICS):
        return token

    compact_name = re.sub(r"[\s　]", "", name)
    is_japanese_name = (
        1 <= len(compact_name) <= 8
        and bool(re.search(r"[一-龥々]", compact_name))
        and bool(re.fullmatch(r"[一-龥々ぁ-んァ-ヶー]+", compact_name))
    )
    is_roman_name = bool(
        re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", name)
        and (" " in name or name.islower())
    )
    looks_like_organization = any(name.endswith(term) for term in INTERNAL_ORGANIZATION_TERMS)
    if not (is_japanese_name or is_roman_name) or looks_like_organization:
        return token
    return f"{name}さん{context}"


def compact_responsible_parts(values: list[Any]) -> list[str]:
    parts: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        for part in re.split(r"\s*/\s*", value):
            cleaned = cleanup_generated_text(part)
            if cleaned:
                parts.append(cleaned)

    compacted: list[str] = []
    for part in parts:
        normalized = normalize_for_search(part)
        if any(normalized == normalize_for_search(existing) for existing in compacted):
            continue
        if any(normalized in normalize_for_search(existing) for existing in compacted):
            continue
        compacted = [existing for existing in compacted if normalize_for_search(existing) not in normalized]
        compacted.append(part)
    return compacted


def build_display_tags(record: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ["customer_industry", "project_or_customer", "department"]:
        value = record.get(key)
        if isinstance(value, str):
            values.extend(extract_keywords(value))
    for key in ["technologies", "tags"]:
        value = record.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value)
    values.extend(infer_keywords_from_text(" ".join([record.get("title", ""), record.get("summary", ""), *values])))
    return normalize_tag_list(values)[:8]


def build_summary(content: str) -> str:
    summary = cleanup_generated_text(content)
    summary = re.sub(r"(?:タイトル|件名)\s*[:：]\s*[^。\n]+(?:。|\n|$)", "", summary).strip()
    summary = re.sub(r"^(?:事例登録|登録|追加|保存)\s*[:：]\s*", "", summary).strip()
    completed = make_natural_sentence(summary if summary else display_content(content))
    return shorten_slack_paragraph(completed, 360)


META_SUMMARY_PATTERN = re.compile(
    r"が正しい"
    r"|(?:を|に|へ)\s*(?:修正|訂正|変更|追記|反映)"
    r"|(?:修正|訂正|追記|変更)\s*(?:します|しました|依頼|内容)"
    r"|既存(?:の)?(?:事例|紹介文|概要)"
    r"|追記\s*[:：]"
)


def looks_like_meta_summary(text: str) -> bool:
    """summary が事例の紹介文ではなく「修正依頼の言い換え」になっていないかを判定する。"""
    return bool(META_SUMMARY_PATTERN.search(text))


def rewrite_updated_summary(existing_summary: str, update_text: str) -> str:
    if not existing_summary or not update_text:
        return ""
    prompt = (
        "社内AI活用事例の紹介文を更新します。既存の紹介文に修正・追記内容を反映した、"
        "更新後の紹介文だけを出力してください。\n"
        "- 1〜2つの完結文で、全体を120文字以内\n"
        "- 文の途中で切らず、末尾に「…」を付けない\n"
        "- 「〜が正しい」「〜を追記」のような修正依頼の言い換えは書かない\n"
        "- 何をした事例かが分かる完成形の文章にする\n\n"
        f"既存の紹介文:\n{existing_summary}\n\n"
        f"修正・追記内容:\n{update_text}"
    )
    try:
        response = bedrock_runtime.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 500},
        )
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        text = "\n".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict)).strip()
    except Exception as exc:
        log_event("summary_rewrite_failed", error_type=type(exc).__name__)
        return ""
    redaction = sanitize_slack_text(text)
    if redaction["status"] == "blocked":
        return ""
    rewritten = build_summary(redaction["text"])
    if not rewritten or looks_like_meta_summary(rewritten):
        log_event("summary_rewrite_rejected", chars=len(rewritten))
        return ""
    log_event("summary_rewrite_succeeded", chars=len(rewritten))
    return rewritten


def merge_case_summary(existing_summary: str, structured_summary: str, update_text: str, case_id: str = "") -> str:
    if structured_summary and not looks_like_meta_summary(structured_summary):
        return structured_summary
    # ツールリトライ等で同じ追記が既に summary に反映済みなら、LLM書き直しも含めて二重適用しない
    # （追記ブロックは build_summary 内の cleanup と同じ整形を通して比較する）
    appended_block = cleanup_generated_text(f"追記: {update_text}")
    if update_text.strip() and appended_block in existing_summary:
        log_event("update_append_duplicate_skipped", case_id=case_id)
        return existing_summary
    rewritten = rewrite_updated_summary(existing_summary, update_text)
    if rewritten:
        return rewritten
    if existing_summary:
        fallback_update = shorten_slack_paragraph(
            make_natural_sentence(cleanup_generated_text(update_text)),
            SUMMARY_FALLBACK_UPDATE_MAX_CHARS,
        )
        if len(fallback_update) > SUMMARY_FALLBACK_UPDATE_MAX_CHARS:
            # 先頭文だけでも長すぎる場合は、資料本文や依頼文を summary に流し込まない。
            # problem / approach / impact 等の構造化項目は別途保存済みなので、既存紹介文を維持する。
            log_event(
                "summary_fallback_update_rejected",
                case_id=case_id,
                chars=len(fallback_update),
            )
            return existing_summary
        return cleanup_generated_text(f"{existing_summary}\n追記: {fallback_update}")
    return cleanup_generated_text(update_text)


def build_case_title(content: str) -> str:
    explicit = re.search(r"(?:タイトル|件名)\s*[:：]\s*([^。\n]+)", content)
    if explicit:
        return truncate(cleanup_generated_text(explicit.group(1)), 64)
    clean_content = cleanup_generated_text(content)
    first_sentence = next((part.strip() for part in re.split(r"[。\n]", clean_content) if part.strip()), clean_content)
    title = re.sub(r"^(事例|取り組み|案件)\s*[:：]?\s*", "", first_sentence)
    title = re.sub(r"(?:を)?(?:登録|追加|保存)(?:して|しといて|してください|お願いします)?$", "", title).strip(" 、。")
    return truncate(title, 64)


def build_search_query(query: str) -> str:
    marker = extract_test_marker(query)
    if marker:
        return marker
    cleaned = SEARCH_PATTERN.sub(" ", normalize_slack_text(query))
    cleaned = re.sub(r"^[\s:：、。-]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if cleaned else normalize_slack_text(query)


def extract_case_id(text: str) -> str | None:
    match = CASE_ID_PATTERN.search(text)
    return match.group(0) if match else None


def extract_test_marker(text: str) -> str | None:
    match = TEST_MARKER_PATTERN.search(text)
    return match.group(0) if match else None


def extract_update_target_hint(text: str) -> str:
    case_id = extract_case_id(text)
    if case_id:
        return case_id

    clean = display_content(text)
    first_sentence = re.split(r"[。\n]", clean, maxsplit=1)[0]
    target = re.sub(r"(?:対応者|担当者|社内担当|担当|種別|タイプ|分類)\s*(?:は|:|：)\s*[^。]+", " ", first_sentence)
    target = re.sub(r"(?:を|の)?(?:事例|案件)?\s*(?:更新|追記|補足|ブラッシュアップ)(?:して|したい|お願いします|して下さい|してください)?", " ", target)
    target = re.sub(r"^(?:この|その|登録済みの?|既存の?事例の?)\s*", " ", target)
    # 一括投入の枕詞（「2026年度上期の社内表彰エントリー資料の内容を」等）がヒントに残ると、
    # 汎用語のトークン一致だけで無関係な事例が上位に来る（2026-08-05 の誤上書きの誘因）
    target = re.sub(r"\d{4}年度\s*\d?Q?の?", " ", target)
    target = re.sub(r"(?:社内表彰|社長賞|エントリー資料|応募資料)(?:の)?", " ", target)
    target = re.sub(r"の内容(?:を)?$", " ", target.strip())
    target = re.sub(r"\s+", " ", target).strip(" 、。:：")
    return target


def extract_case_attributes(text: str) -> dict[str, str]:
    clean = display_content(text)
    attributes: dict[str, str] = {}

    owner_match = re.search(r"(?:対応者|担当者|社内担当|担当)\s*(?:は|:|：)\s*([^。]+)", clean)
    if owner_match:
        owner_value = clean_attribute_value(owner_match.group(1))
        department, name = split_department_and_name(owner_value)
        if department:
            attributes["owner_department"] = department
        if name:
            attributes["owner_name"] = name

    type_match = re.search(r"(?:種別|タイプ|分類)\s*(?:は|:|：)\s*([^。、]+)", clean)
    if type_match:
        case_type = clean_attribute_value(type_match.group(1))
        if case_type:
            attributes["case_type"] = case_type

    team_match = re.search(r"(?:チーム|担当チーム)\s*(?:は|:|：)\s*([^。、]+)", clean)
    if team_match:
        team = clean_attribute_value(team_match.group(1))
        if team:
            attributes["team"] = team

    return attributes


def clean_attribute_value(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" 、。:：")
    value = re.sub(r"(?:です|でした|である|になります|ですので)$", "", value).strip(" 、。:：")
    return value


def split_department_and_name(value: str) -> tuple[str | None, str | None]:
    if "の" not in value:
        return None, value or None
    department, name = value.rsplit("の", 1)
    return department.strip() or None, name.strip() or None


def format_attributes(attributes: dict[str, Any]) -> str:
    parts: list[str] = []
    if attributes.get("customer_industry"):
        parts.append(f"業界: {attributes['customer_industry']}")
    responsible = format_responsible_value(attributes)
    if responsible:
        parts.append(f"担当: {responsible}")
    if attributes.get("impact"):
        parts.append(f"効果: {attributes['impact']}")
    return " / ".join(parts)


def format_source_url_bullets(record: dict[str, Any], indent: str = "") -> list[str]:
    return []


def display_content(text: str) -> str:
    text = normalize_slack_text(text)
    text = TEST_MARKER_PATTERN.sub("", text)
    text = re.sub(r"これはE2Eテスト用の架空データです[。.]?", "", text)
    text = re.sub(r"E2Eテスト用の架空データです[。.]?", "", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_keywords(content: str) -> list[str]:
    normalized = normalize_slack_text(content)
    words = [
        word.strip()
        for word in re.split(r"[\s、。・,./:：;；()[\]「」『』【】]+", normalized)
        if 2 <= len(word.strip()) <= 48
    ]
    return list(dict.fromkeys(words))[:30]


def score_case(record: dict[str, Any], query: str, search_terms: list[str] | None = None) -> int:
    normalized_query = normalize_for_search(query)
    expanded_terms = list(dict.fromkeys([*tokenize_for_search(query), *(search_terms or []), *infer_keywords_from_text(query)]))
    record_text = build_record_search_text(record)
    inferred_record_keywords = infer_keywords_from_text(record_text)
    haystack = normalize_for_search(
        " ".join([record_text, *inferred_record_keywords])
    )
    score = 0
    if len(normalized_query) >= 2 and normalized_query in haystack:
        score += 100
    for raw_token in expanded_terms:
        token = normalize_for_search(raw_token)
        if len(token) < 2:
            continue
        if record.get("test_marker") and normalize_for_search(record["test_marker"]) == token:
            score += 80
        if token in normalize_for_search(record.get("title", "")):
            score += 20
        if token in normalize_for_search(record.get("summary", "")):
            score += 8
        if token in normalize_for_search(" ".join(str(record.get(field, "")) for field in ["owner_department", "owner_name", "case_type", "team", "client", "project", "project_or_customer", "customer_industry", "department", "contact_hint", "problem", "approach", "impact"])):
            score += 10
        if any(token in normalize_for_search(keyword) for keyword in [*record.get("keywords", []), *record.get("technologies", []), *record.get("tags", []), *inferred_record_keywords]):
            score += 6
    return score


def build_record_search_text(record: dict[str, Any]) -> str:
    return " ".join(
        [
            record.get("title", ""),
            record.get("summary", ""),
            record.get("case_id", ""),
            record.get("test_marker", ""),
            record.get("owner_department", ""),
            record.get("owner_name", ""),
            record.get("case_type", ""),
            record.get("team", ""),
            record.get("client", ""),
            record.get("project", ""),
            record.get("project_or_customer", ""),
            record.get("customer_industry", ""),
            record.get("department", ""),
            record.get("contact_hint", ""),
            record.get("problem", ""),
            record.get("approach", ""),
            record.get("impact", ""),
            *record.get("keywords", []),
            *record.get("technologies", []),
            *record.get("tags", []),
        ]
    )


def rank_search_results(records: list[dict[str, Any]], query: str, search_terms: list[str] | None = None) -> list[dict[str, Any]]:
    candidates = sorted(
        (
            {"record": record, "score": score_case(record, query, search_terms)}
            for record in records
        ),
        key=lambda item: (-item["score"], item["record"].get("updated_at", "")),
    )
    candidates = [item for item in candidates if item["score"] > 0]
    if not candidates:
        return []

    top_score = candidates[0]["score"]
    return [
        item
        for item in candidates
        if is_relevant_search_result(item["record"], query, search_terms or [], item["score"], top_score)
    ][:MAX_SEARCH_RESULTS]


def retrieve_cases_from_knowledge_base(query: str, number_of_results: int, record_call: bool = False) -> dict[str, Any]:
    if record_call:
        record_tool_call("retrieve_cases_tool")

    if not KNOWLEDGE_BASE_ID:
        return {"ok": False, "error": "knowledge_base_not_configured", "results": []}

    redaction = sanitize_slack_text(query)
    if redaction["status"] == "blocked":
        log_event("knowledge_base_retrieve_blocked")
        return {"ok": False, "error": "query_contains_blocked_information", "results": []}

    query_text = truncate(cleanup_generated_text(str(redaction["text"])), 1000)
    if not query_text:
        return {"ok": False, "error": "empty_query", "results": []}

    result_limit = clamp_int(number_of_results, default=CASE_KNOWLEDGE_RETRIEVAL_RESULTS, minimum=1, maximum=8)
    try:
        response = bedrock_agent_runtime.retrieve(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            retrievalQuery={"text": query_text},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": result_limit,
                }
            },
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", type(exc).__name__)
        log_event("knowledge_base_retrieve_failed", error_code=error_code)
        return {"ok": False, "error": "knowledge_base_retrieve_failed", "results": []}

    results: list[dict[str, Any]] = []
    retrieved_ids: list[str] = []
    for item in response.get("retrievalResults", []):
        content = str(item.get("content", {}).get("text", ""))
        case_ids = extract_case_ids_from_retrieval_result(item, content)
        for case_id in case_ids:
            if case_id not in retrieved_ids:
                retrieved_ids.append(case_id)
        results.append(
            {
                "case_ids": case_ids,
                "score": item.get("score"),
                "excerpt": truncate(sanitize_slack_text(content).get("text", ""), 900),
            }
        )

    _retrieved_case_ids.set(merge_case_ids(_retrieved_case_ids.get(), retrieved_ids))
    _retrieval_results.set(results)
    log_event("knowledge_base_retrieved", result_count=len(results), case_ids=_retrieved_case_ids.get())
    return {
        "ok": True,
        "query": query_text,
        "results": results,
    }


def extract_case_ids_from_retrieval_result(item: dict[str, Any], content: str) -> list[str]:
    candidates = [content]
    location = item.get("location")
    if isinstance(location, dict):
        candidates.append(json.dumps(location, ensure_ascii=False))
    ids: list[str] = []
    for candidate in candidates:
        for match in CASE_ID_PATTERN.findall(candidate):
            if match not in ids:
                ids.append(match)
    return ids


def merge_case_ids(current: list[str], incoming: list[str]) -> list[str]:
    merged = list(current)
    for case_id in incoming:
        if case_id not in merged:
            merged.append(case_id)
    return merged


def semantic_score_by_case_id() -> dict[str, float]:
    """Knowledge Base retrieve の結果から、case_id ごとの最高スコアを引けるようにする。"""
    scores: dict[str, float] = {}
    for item in _retrieval_results.get():
        raw_score = item.get("score")
        if not isinstance(raw_score, (int, float)):
            continue
        for case_id in item.get("case_ids", []):
            scores[case_id] = max(scores.get(case_id, 0.0), float(raw_score))
    return scores


def looks_like_ai_related_topic(text: str) -> bool:
    return bool(AI_TOPIC_PATTERN.search(text))


def semantic_records_from_retrieval(records: list[dict[str, Any]], query: str, search_terms: list[str]) -> list[dict[str, Any]]:
    retrieved_ids = _retrieved_case_ids.get()
    if not retrieved_ids:
        return []

    record_by_id = {record.get("case_id"): record for record in records}
    requested_categories = requested_specific_search_categories(query, search_terms)
    semantic_scores = semantic_score_by_case_id()
    results: list[dict[str, Any]] = []
    for case_id in retrieved_ids:
        record = record_by_id.get(case_id)
        if not record:
            continue
        # ベクトル検索は無関係な質問にも最近傍を返すため、スコアの足切りで除外する
        if semantic_scores.get(case_id, 0.0) < KB_MIN_SEMANTIC_SCORE:
            continue
        if any(not record_matches_search_category(record, category) for category in requested_categories):
            continue
        results.append(record)
    return results


def hint_records_from_retrieval(
    records: list[dict[str, Any]], query: str, search_terms: list[str]
) -> list[dict[str, Any]]:
    """足切りには届かないが無関係とも言い切れない候補を、ヒント提示用に最大2件返す。

    _retrieved_case_ids は Knowledge Base のスコア降順なので、その順序を保って選ぶ。
    """
    retrieved_ids = _retrieved_case_ids.get()
    if not retrieved_ids:
        return []

    record_by_id = {record.get("case_id"): record for record in records}
    requested_categories = requested_specific_search_categories(query, search_terms)
    semantic_scores = semantic_score_by_case_id()
    hints: list[dict[str, Any]] = []
    for case_id in retrieved_ids:
        record = record_by_id.get(case_id)
        if not record:
            continue
        if semantic_scores.get(case_id, 0.0) < KB_HINT_SEMANTIC_SCORE:
            continue
        if any(not record_matches_search_category(record, category) for category in requested_categories):
            continue
        hints.append(record)
        if len(hints) >= MAX_HINT_RESULTS:
            break
    return hints


def classify_search_confidence(
    record: dict[str, Any],
    query: str,
    search_terms: list[str],
    semantic_scores: dict[str, float],
) -> str:
    case_id = str(record.get("case_id") or "")
    if semantic_scores.get(case_id, 0.0) >= KB_CONFIDENT_SEMANTIC_SCORE:
        return "strong"
    if score_case(record, query, search_terms) >= STRONG_LEXICAL_SCORE:
        return "strong"
    return "weak"


def order_results_by_confidence(
    results: list[dict[str, Any]], query: str, search_terms: list[str]
) -> tuple[list[dict[str, Any]], str]:
    """確信できる一致を先頭へ寄せ、返信全体の確信度（strong / weak / none）を決める。"""
    if not results:
        return [], "none"
    semantic_scores = semantic_score_by_case_id()
    strong: list[dict[str, Any]] = []
    weak: list[dict[str, Any]] = []
    for record in results:
        if classify_search_confidence(record, query, search_terms, semantic_scores) == "strong":
            strong.append(record)
        else:
            weak.append(record)
    return [*strong, *weak], ("strong" if strong else "weak")


def merge_search_records(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in [*primary, *secondary]:
        case_id = record.get("case_id")
        if not isinstance(case_id, str) or case_id in seen:
            continue
        seen.add(case_id)
        results.append(record)
        if len(results) >= MAX_SEARCH_RESULTS:
            break
    return results


def is_relevant_search_result(record: dict[str, Any], query: str, search_terms: list[str], score: int, top_score: int) -> bool:
    if score < MIN_SEARCH_SCORE:
        return False

    relative_floor = max(MIN_SEARCH_SCORE, int(top_score * MIN_RELATIVE_SEARCH_SCORE))
    if score < relative_floor:
        return False

    requested_categories = requested_specific_search_categories(query, search_terms)
    return all(record_matches_search_category(record, category) for category in requested_categories)


def requested_specific_search_categories(query: str, search_terms: list[str]) -> set[str]:
    query_text = normalize_for_search(" ".join([query, *search_terms]))
    return {
        category
        for category, terms in SPECIFIC_SEARCH_CATEGORY_TERMS.items()
        if any(normalize_for_search(term) in query_text for term in terms)
    }


def record_matches_search_category(record: dict[str, Any], category: str) -> bool:
    record_text = build_record_search_text(record)
    haystack = normalize_for_search(" ".join([record_text, *infer_keywords_from_text(record_text)]))
    return any(normalize_for_search(term) in haystack for term in SPECIFIC_SEARCH_CATEGORY_TERMS.get(category, []))


def rank_cases(query: str, search_terms: list[str] | None = None) -> list[dict[str, Any]]:
    return [
        item
        for item in sorted(
            (
                {"record": record, "score": score_case(record, query, search_terms)}
                for record in scan_active_cases()
            ),
            key=lambda item: (-item["score"], item["record"].get("updated_at", "")),
        )
        if item["score"] > 0
    ]


def should_auto_update(candidates: list[dict[str, Any]]) -> bool:
    """更新対象を確認なしで確定してよいかを判定する。

    更新は既存の構造化項目を置き換えるため、取り違えるとその事例の内容が失われる。
    弱い一致や僅差の1位では確定せず、確認待ちへ倒す（fail-safe）。
    """
    if not candidates:
        return False
    top_score = candidates[0]["score"]
    # 候補が1件でも、ヒントとの一致が弱ければ確定しない。
    # 汎用語がいくつか当たっただけの事例を「唯一の候補」として書き換えないため。
    if top_score < AUTO_UPDATE_MIN_TOP_SCORE:
        return False
    if len(candidates) == 1:
        return True
    second_score = candidates[1]["score"]
    if second_score <= 0:
        return True
    return top_score - second_score >= AUTO_UPDATE_SCORE_GAP and top_score >= second_score * AUTO_UPDATE_SCORE_RATIO


QUOTED_TITLE_PATTERN = re.compile(r"[「『\"“”']([^「」『』\"“”']{4,120})[」』\"“”']")


def find_cases_by_quoted_title(text: str) -> list[dict[str, Any]]:
    """本文の鉤括弧・引用符の中に既存事例のタイトルがそのまま書かれていれば、その事例を返す。

    利用者が正式タイトルを引用して対象を指名した場合は、曖昧なスコアリングより
    その指名を優先する。2026-08-05 の誤上書きでは、指名したタイトルが順位付けに
    一切反映されず、定型句のトークン一致で別事例が選ばれていた。
    """
    # LLM はツール引数の本文を要約・言い換えするため、利用者が引用したタイトルが
    # additional_content から消えることがある（2026-08-05 の検証で実測）。
    # 指名の根拠は利用者が実際に書いた原文にあるので、payload の本文も必ず併せて見る。
    payload_text = normalize_slack_text(str(_current_payload.get().get("text", "")))
    haystack = " ".join(part for part in [display_content(text), display_content(payload_text)] if part)
    quoted = [match.group(1).strip() for match in QUOTED_TITLE_PATTERN.finditer(haystack)]
    normalized_quoted = {normalize_for_search(value) for value in quoted if len(value) >= 4}
    normalized_quoted.discard("")
    if not normalized_quoted:
        return []
    matched = [
        record
        for record in scan_active_cases()
        if normalize_for_search(record.get("title", "")) in normalized_quoted
    ]
    # 同一タイトルが複数あることは想定しないが、あった場合は確定させず呼び出し側で確認へ倒す
    return matched


def select_candidate(selection: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    case_id = extract_case_id(selection)
    if case_id:
        return next((record for record in candidates if record.get("case_id") == case_id), None)
    number_match = re.search(r"([1-9])\s*(?:番|件目|つ目)?", normalize_slack_text(selection))
    if number_match:
        index = int(number_match.group(1)) - 1
        if 0 <= index < len(candidates):
            return candidates[index]
    if len(candidates) == 1 and normalize_slack_text(selection):
        return candidates[0]

    ranked = sorted(
        ({"record": record, "score": score_case(record, selection)} for record in candidates),
        key=lambda item: -item["score"],
    )
    return ranked[0]["record"] if ranked and ranked[0]["score"] > 0 else None


def looks_like_update_content(text: str) -> bool:
    return bool(re.search(r"(対応者|担当者|社内担当|担当|種別|タイプ|分類|追記|補足|効果|部署|チーム)", normalize_slack_text(text)))


def format_search_response(
    results: list[dict[str, Any]],
    query: str,
    search_terms: list[str],
    confidence: str = "strong",
    scope_note: str = "",
) -> str:
    primary = results[0]
    title = cleanup_generated_text(str(primary.get("title") or "社内AI活用事例"))

    normalized_question = normalize_slack_text(query)
    primary_lines = (
        build_case_detail_lines(primary, normalized_question)
        if is_case_deep_dive_question(normalized_question) and not SEARCH_REFINEMENT_PATTERN.search(normalized_question)
        else [format_case_paragraph(primary, max_length=CASE_BODY_MAX_CHARS)]
    )
    if confidence != "strong":
        # しきい値ぎりぎりの一致しか無いときは「はい、あります」と断定しない
        answer = f"ドンピシャの登録事例は見つかりませんでした。近いところだと「{title}」があります。"
    elif SEARCH_REFINEMENT_PATTERN.search(normalized_question):
        answer = f"それなら、「{title}」が近そうです。"
    elif SEARCH_EXISTENCE_QUESTION_PATTERN.search(normalized_question):
        answer = f"はい、あります。「{title}」が近そうです。"
    else:
        answer = f"「{title}」が近そうです。"

    alternatives = results[1:MAX_SEARCH_RESULTS]
    if alternatives:
        alternative_lines = [
            f"ほかには{format_title_list(alternatives)}もあります。気になるものがあれば、このスレッドで Bot にメンションして聞いてください。",
        ]
    else:
        alternative_lines = ["気になる点があれば、このスレッドで Bot にメンションして聞いてください。"]
    scope_lines = [scope_note] if scope_note else []
    return format_slack_sections(answer, *primary_lines, alternative_lines, scope_lines)


def build_match_reason(record: dict[str, Any], query: str, search_terms: list[str]) -> str:
    candidates = [query, *search_terms, *infer_keywords_from_text(query)]
    haystack_fields = [
        ("業界", record.get("customer_industry")),
        ("案件・顧客", record.get("project_or_customer")),
        ("課題", record.get("problem")),
        ("取り組み", record.get("approach")),
        ("効果", record.get("impact")),
    ]
    reasons: list[str] = []
    for label, value in haystack_fields:
        if not isinstance(value, str) or not value:
            continue
        normalized_value = normalize_for_search(value)
        if any(normalize_for_search(term) in normalized_value or normalized_value in normalize_for_search(term) for term in candidates if term):
            reasons.append(f"{label}が近い")
    if not reasons:
        tags = [str(tag) for tag in [*record.get("keywords", []), *record.get("tags", []), *record.get("technologies", [])]]
        matched = [tag for tag in tags if any(normalize_for_search(term) in normalize_for_search(tag) for term in candidates if term)]
        if matched:
            reasons.append(f"タグ: {', '.join(matched[:3])}")
    return " / ".join(list(dict.fromkeys(reasons))[:3])


def format_case_detail(record: dict[str, Any], question: str = "", answer: str = "") -> str:
    title = cleanup_generated_text(str(record.get("title") or "社内AI活用事例"))
    detail_lines = build_case_detail_lines(record, question, answer)
    detail_lines.extend(format_source_url_bullets(record))
    return format_slack_sections(f"「{title}」ですね。", *detail_lines)


def is_case_graphic_request(question: str) -> bool:
    return bool(CASE_GRAPHIC_REQUEST_PATTERN.search(normalize_slack_text(question)))


def is_case_next_action_request(question: str) -> bool:
    return bool(CASE_NEXT_ACTION_PATTERN.search(normalize_slack_text(question)))


def collect_recent_case_questions(payload: dict[str, Any]) -> str:
    """直前に利用者が深掘りした観点を、橋渡し時の具体的な質問テーマへ引き継ぐ。"""
    messages = payload.get("thread_messages", [])
    if not isinstance(messages, list):
        return ""
    questions: list[str] = []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("bot_id") or message.get("is_current"):
            continue
        text = normalize_slack_text(str(message.get("text", "")))
        if not text or is_case_next_action_request(text) or not is_case_deep_dive_question(text):
            continue
        questions.append(text)
        if len(questions) >= 3:
            break
    return "\n".join(reversed(questions))


def extract_case_action_subject(question: str) -> str:
    """「高橋さんはどうする？」のような第三者主語を、自然な返信に引き継ぐ。"""
    normalized = normalize_slack_text(question)
    match = re.search(r"([^\s、。！？?]{1,24}さん)(?=は|なら)", normalized)
    if not match:
        return ""
    subject = match.group(1)
    subject = re.sub(r"^(?:このあと|この後|ここから|今後|次に|じゃあ|では|それなら)+", "", subject)
    return cleanup_generated_text(subject) if 2 <= len(subject) <= 20 else ""


def format_case_next_action(record: dict[str, Any], question: str = "", context_question: str = "") -> str:
    """登録情報を会話の行き止まりにせず、具体的なSlack相談へつなげる。"""
    contact = format_internal_people_with_honorifics(record.get("contact_hint") or record.get("owner_name"))
    destinations = compact_responsible_parts([
        record.get("owner_department"),
        record.get("department"),
        record.get("team"),
    ])
    department = next(
        (part for part in destinations if normalize_for_search(part) not in normalize_for_search(contact)),
        "",
    )
    topic = format_case_handoff_topic(context_question or question)
    subject = extract_case_action_subject(question)
    lead = f"{subject}は、まず" if subject else "次は、"

    if contact:
        destination = f"{contact}か「{department}」のメンバー" if department else contact
        action = f"{lead}{destination}にSlackで、{topic}を聞いてみるのがよさそうです。"
    elif department:
        action = f"{lead}「{department}」のメンバーにSlackで、{topic}を聞いてみるのがよさそうです。"
    else:
        title = cleanup_generated_text(str(record.get("title") or "この事例"))
        action = (
            f"{lead}このチャンネルで「{title}について、{topic}を知りたい」と添えて、"
            "詳しい方を聞いてみるのがよさそうです。"
        )

    role = "このBotで登録済みの概要をつかんだ後、詳しい人との会話につなげてもらうのが一番確実です。"
    return format_slack_sections(action, role)


def is_case_deep_dive_question(question: str) -> bool:
    normalized = normalize_slack_text(question)
    return any(
        pattern.search(normalized)
        for pattern in [
            DETAIL_PATTERN,
            CASE_PRODUCT_QUESTION_PATTERN,
            CASE_IMPACT_QUESTION_PATTERN,
            CASE_APPROACH_QUESTION_PATTERN,
            CASE_PROBLEM_QUESTION_PATTERN,
            CASE_TECHNOLOGY_QUESTION_PATTERN,
            CASE_CONTACT_QUESTION_PATTERN,
        ]
    )


def build_case_detail_lines(record: dict[str, Any], question: str = "", answer: str = "") -> list[str]:
    direct_answer = sanitize_case_answer(answer)
    answer_lines = [direct_answer] if direct_answer else build_grounded_case_answer_lines(record, question)
    return [*answer_lines, format_case_contact_guidance(record, question)]


def sanitize_case_answer(answer: str) -> str:
    cleaned = normalize_slack_text(answer)
    if not cleaned:
        return ""
    sanitized = sanitize_slack_text(cleaned)
    if sanitized["status"] == "blocked":
        return ""
    return format_slack_sections(sanitized["text"].strip())


def build_grounded_case_answer_lines(record: dict[str, Any], question: str) -> list[str]:
    normalized = normalize_slack_text(question)
    requested_product = bool(CASE_PRODUCT_QUESTION_PATTERN.search(normalized))
    requested_impact = bool(CASE_IMPACT_QUESTION_PATTERN.search(normalized))
    requested_approach = bool(CASE_APPROACH_QUESTION_PATTERN.search(normalized))
    requested_problem = bool(CASE_PROBLEM_QUESTION_PATTERN.search(normalized))
    requested_technology = bool(CASE_TECHNOLOGY_QUESTION_PATTERN.search(normalized))

    if not any([requested_product, requested_impact, requested_approach, requested_problem, requested_technology]):
        return [format_case_paragraph(record, max_length=CASE_BODY_MAX_CHARS)]

    lines: list[str] = []
    if requested_product:
        product_fact = find_product_fact(record)
        if product_fact:
            lines.append(make_natural_sentence(f"作っているものについては、{strip_sentence_ending(product_fact)}"))
        else:
            lines.append("この事例が何を作る案件なのか、プロダクトの中身までは登録情報にありませんでした。")

    if requested_impact:
        impact = cleanup_generated_text(str(record.get("impact") or "")) or find_metric_fact(record)
        if impact:
            lines.append(format_impact_detail(impact))
        else:
            lines.append("定量効果の詳しい数字は、登録情報では確認できませんでした。")

    if requested_problem:
        problem = cleanup_generated_text(str(record.get("problem") or ""))
        lines.append(
            make_natural_sentence(f"背景には、{strip_sentence_ending(problem)}")
            if problem
            else "取り組みの背景や元の課題は、登録情報では確認できませんでした。"
        )

    if requested_approach:
        approach = cleanup_generated_text(str(record.get("approach") or ""))
        lines.append(
            make_natural_sentence(f"進め方としては、{strip_sentence_ending(approach)}")
            if approach
            else "具体的な進め方は、登録情報では確認できませんでした。"
        )

    if requested_technology:
        technologies = normalize_string_list(record.get("technologies") or [])
        if technologies:
            lines.append(f"使っている技術として、{'、'.join(technologies)}が登録されています。")
        else:
            lines.append("使っている技術の詳しい構成は、登録情報では確認できませんでした。")

    return lines


def find_product_fact(record: dict[str, Any]) -> str:
    for value in [record.get("summary"), record.get("approach"), record.get("problem")]:
        if not isinstance(value, str):
            continue
        for sentence in split_complete_sentences(value):
            if CASE_PRODUCT_FACT_PATTERN.search(sentence):
                return sentence
    return ""


def find_metric_fact(record: dict[str, Any]) -> str:
    for value in [record.get("summary"), record.get("approach")]:
        if not isinstance(value, str):
            continue
        for sentence in split_complete_sentences(value):
            if CASE_METRIC_FACT_PATTERN.search(sentence):
                return sentence
    return ""


def split_complete_sentences(value: str) -> list[str]:
    cleaned = cleanup_generated_text(value)
    sentences = [match.group(0).strip() for match in re.finditer(r"[^。！？!?]+[。！？!?]?", cleaned)]
    return [sentence for sentence in sentences if sentence]


def strip_sentence_ending(value: str) -> str:
    return cleanup_generated_text(value).rstrip("。！？!? ")


def format_impact_detail(value: str) -> str:
    fact = strip_sentence_ending(value)
    if re.search(r"(?:削減|短縮|向上|改善|増加|減少|半減|倍増)$", fact):
        return f"効果については、{fact}したと記録されています。"
    if fact.endswith(("です", "ます", "でした", "ました", "しています", "している")):
        return make_natural_sentence(f"効果については、{fact}")
    return f"効果については、{fact}と記録されています。"


def format_case_contact_guidance(record: dict[str, Any], question: str = "") -> str:
    contact = format_internal_people_with_honorifics(record.get("contact_hint") or record.get("owner_name"))
    destinations = compact_responsible_parts([
        record.get("owner_department"),
        record.get("department"),
        record.get("team"),
    ])
    department = next(
        (part for part in destinations if normalize_for_search(part) not in normalize_for_search(contact)),
        "",
    )
    topic = format_case_handoff_topic(question)
    if contact:
        destination = f"{contact}か「{department}」" if department else contact
        return f"{topic}まで知りたい場合は、Slackで{destination}に聞いてみてください。"
    if department:
        return f"{topic}まで知りたい場合は、Slackで「{department}」のメンバーに聞いてみてください。"
    title = cleanup_generated_text(str(record.get("title") or "この事例"))
    return f"この事例には聞き先がまだ登録されていません。「{title}」と添えて、このチャンネルで詳しい方を聞いてみてください。"


def format_case_handoff_topic(question: str) -> str:
    normalized = normalize_slack_text(question)
    product = bool(CASE_PRODUCT_QUESTION_PATTERN.search(normalized))
    impact = bool(CASE_IMPACT_QUESTION_PATTERN.search(normalized))
    if product and impact:
        return "プロダクトの中身や定量効果の背景"
    if product:
        return "プロダクトの中身や案件の背景"
    if impact:
        return "効果の測り方や現場での実感"
    if CASE_APPROACH_QUESTION_PATTERN.search(normalized):
        return "具体的な進め方や実装の工夫"
    if CASE_PROBLEM_QUESTION_PATTERN.search(normalized):
        return "取り組みの背景や課題の詳しい事情"
    if CASE_TECHNOLOGY_QUESTION_PATTERN.search(normalized):
        return "技術選定や構成の詳しい理由"
    return "登録情報より踏み込んだ内容"


def is_case_detail_link_request(question: str) -> bool:
    return bool(CASE_DETAIL_LINK_REQUEST_PATTERN.search(normalize_slack_text(question)))


def format_case_link_unavailable_reply(record: dict[str, Any]) -> str:
    limitation = "このアプリでは、事例の詳細リンクを取得する機能はまだありません。"
    return format_slack_sections(limitation, format_case_contact_guidance(record, "詳細リンクを知りたい"))


def format_daily_share(record: dict[str, Any]) -> str:
    share_lines = [format_daily_case_paragraph(record, max_length=DAILY_SHARE_BODY_MAX_CHARS)]
    share_lines.extend(format_source_url_bullets(record))
    return format_slack_sections(format_daily_case_intro(record), share_lines)


def get_case_by_id(case_id: str) -> dict[str, Any] | None:
    response = cases_table.get_item(Key={"case_id": case_id})
    record = response.get("Item")
    if record and record.get("status") != "archived":
        return record
    return None


def get_cases_by_ids(case_ids: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for case_id in case_ids:
        record = get_case_by_id(case_id)
        if record:
            records.append(record)
    return records


def format_candidate_lines(records: list[dict[str, Any]]) -> list[str]:
    candidates: list[str] = []
    for index, record in enumerate(records, 1):
        title = cleanup_generated_text(str(record.get("title") or "事例"))
        responsible = format_responsible_value(record)
        if responsible:
            candidates.append(f"{index}番は「{title}」（担当: {responsible}）")
        else:
            candidates.append(f"{index}番は「{title}」")
    if not candidates:
        return []
    return [f"候補は{ '、'.join(candidates) }です。"]


def tokenize_for_search(query: str) -> list[str]:
    normalized = normalize_for_search(query)
    tokens = [
        token
        for token in re.split(r"[\s、。・,./:：;；()[\]「」『』【】]+", normalized)
        if len(token) >= 2
    ]
    tokens.extend(re.findall(r"[a-z0-9_-]{2,}", normalized))
    return list(dict.fromkeys(tokens))


def normalize_for_search(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def normalize_case_similarity_key(text: str) -> str:
    # 空白・記号を除去して表記ゆれ（全半角スペース、括弧、E2Eマーカー等の区切り）を吸収する
    return re.sub(r"[\W_]+", "", text.lower())


def find_duplicate_case_candidates(title: str, project_or_customer: str) -> list[dict[str, Any]]:
    """登録直前の重複チェック。タイトル類似度と案件名一致で既存事例を探す。

    retrieve_cases_tool（Knowledge Base）は同期ラグがあり直前登録を見逃すため、
    DynamoDB を直接スキャンして即時整合で判定する。
    """
    normalized_title = normalize_case_similarity_key(title)
    if not normalized_title:
        return []
    normalized_project = normalize_case_similarity_key(project_or_customer)

    candidates: list[dict[str, Any]] = []
    for record in scan_active_cases():
        existing_title = normalize_case_similarity_key(str(record.get("title") or ""))
        if not existing_title:
            continue
        ratio = difflib.SequenceMatcher(None, normalized_title, existing_title).ratio()
        if ratio >= 0.8 or normalized_title in existing_title or existing_title in normalized_title:
            candidates.append(record)
            continue
        if normalized_project and ratio >= 0.6:
            existing_project = normalize_case_similarity_key(str(record.get("project_or_customer") or ""))
            if existing_project and existing_project == normalized_project:
                candidates.append(record)
    candidates.sort(key=lambda record: str(record.get("updated_at") or ""), reverse=True)
    return candidates


def merge_redaction_status(current: str, incoming: str) -> str:
    if current == "masked" or incoming == "masked":
        return "masked"
    if current == "needs_review" or incoming == "needs_review":
        return "needs_review"
    return "safe"


def scan_active_cases() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {"Limit": 100}
    while True:
        response = cases_table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            if item.get("status") != "archived":
                records.append(item)
            if len(records) >= MAX_SEARCH_SCAN_ITEMS:
                return records
        if "LastEvaluatedKey" not in response:
            return records
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def payload_channel_id(payload: dict[str, Any]) -> str:
    # channel_id 欠落時に固定チャンネルへフォールバックすると、DM や別チャンネルの
    # thread_state が混線するため、欠落時は空文字を返して呼び出し側でスキップする
    return str(payload.get("channel_id") or "").strip()


def get_current_thread_state(payload: dict[str, Any]) -> dict[str, Any] | None:
    channel_id = payload_channel_id(payload)
    thread_ts = payload.get("thread_ts", payload.get("message_ts", ""))
    if not channel_id or not thread_ts:
        return None
    response = thread_state_table.get_item(Key={"thread_key": build_thread_key(channel_id, thread_ts)})
    # boto3 の DynamoDB 読み取りは数値を Decimal で返し、そのまま build_prompt の
    # json.dumps に流すと TypeError で invoke ごと落ちる（last_url_fetch_results の
    # status_code 等で 2026-07-09 に実障害）ため、読み取り境界で必ず変換する
    return decimal_to_native(response.get("Item"))


def decimal_to_native(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: decimal_to_native(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decimal_to_native(item) for item in value]
    return value


def summarize_thread_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    summary: dict[str, Any] | None = None
    if state.get("pending_action") == "update_case":
        summary = {
            "pending_action": "update_case",
            "target_hint": state.get("target_hint", ""),
            "candidate_case_ids": state.get("candidate_case_ids", []),
            "candidate_titles": state.get("candidate_titles", []),
        }
    elif state.get("last_search_case_ids"):
        summary = {
            "last_search_query": state.get("last_search_query", ""),
            "last_search_case_ids": state.get("last_search_case_ids", []),
            "last_search_titles": state.get("last_search_titles", []),
        }
    elif state.get("case_id"):
        summary = {"case_id": state["case_id"]}
    if state.get("last_url_fetch_results"):
        summary = summary or {}
        summary["last_url_fetch_results"] = state["last_url_fetch_results"]
    return summary


def summarize_thread_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    summarized: list[dict[str, Any]] = []
    for message in messages[-12:]:
        if not isinstance(message, dict):
            continue
        text = normalize_slack_text(str(message.get("text", "")))
        if not text:
            continue
        summarized.append(
            {
                "ts": message.get("ts"),
                "speaker": "bot" if message.get("bot_id") else "user",
                "is_current": bool(message.get("is_current")),
                "text": truncate(text, 700),
            }
        )
    return summarized


def save_pending_update(additional_content: str, target_hint: str, candidates: list[dict[str, Any]]) -> None:
    payload = _current_payload.get()
    channel_id = payload_channel_id(payload)
    thread_ts = payload.get("thread_ts", payload.get("message_ts", ""))
    if not channel_id or not thread_ts:
        return
    now = utc_now_iso()
    thread_state_table.put_item(
        Item={
            "thread_key": build_thread_key(channel_id, thread_ts),
            "pending_action": "update_case",
            "additional_content": additional_content,
            "target_hint": target_hint,
            "candidate_case_ids": [record["case_id"] for record in candidates],
            "candidate_titles": [record["title"] for record in candidates],
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "updated_at": now,
            "expires_at": epoch_seconds() + THREAD_STATE_TTL_SECONDS,
        }
    )


def save_search_state(query: str, records: list[dict[str, Any]]) -> None:
    payload = _current_payload.get()
    channel_id = payload_channel_id(payload)
    thread_ts = payload.get("thread_ts", payload.get("message_ts", ""))
    if not channel_id or not thread_ts:
        return
    now = utc_now_iso()
    thread_state_table.put_item(
        Item={
            "thread_key": build_thread_key(channel_id, thread_ts),
            "last_search_query": query,
            "last_search_case_ids": [record["case_id"] for record in records],
            "last_search_titles": [record["title"] for record in records],
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "updated_at": now,
            "expires_at": epoch_seconds() + THREAD_STATE_TTL_SECONDS,
        }
    )


def save_thread_state(record: dict[str, Any]) -> None:
    payload = _current_payload.get()
    channel_id = payload_channel_id(payload)
    thread_ts = payload.get("thread_ts", payload.get("message_ts", ""))
    if not channel_id or not thread_ts:
        return
    item: dict[str, Any] = {
        "thread_key": build_thread_key(channel_id, thread_ts),
        "case_id": record["case_id"],
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "updated_at": record["updated_at"],
        "expires_at": epoch_seconds() + THREAD_STATE_TTL_SECONDS,
    }
    url_fetch_results = compact_url_fetch_results()
    if url_fetch_results:
        item["last_url_fetch_results"] = url_fetch_results
    thread_state_table.put_item(Item=item)


def delete_thread_states_for_case(case_id: str) -> None:
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": Attr("case_id").eq(case_id),
        "ProjectionExpression": "thread_key",
    }
    while True:
        response = thread_state_table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            if isinstance(item.get("thread_key"), str):
                thread_state_table.delete_item(Key={"thread_key": item["thread_key"]})
        if "LastEvaluatedKey" not in response:
            return
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def schedule_case_graphic(record: dict[str, Any]) -> dict[str, Any]:
    if not GRAPHIC_GENERATION_ENABLED:
        return record

    now = utc_now_iso()
    due_at = utc_iso_after(GRAPHIC_GENERATION_DELAY_SECONDS)
    updates = {
        "graphic_status": "pending",
        "graphic_requested_at": now,
        "graphic_due_at": due_at,
        "graphic_due_epoch": epoch_seconds() + GRAPHIC_GENERATION_DELAY_SECONDS,
        "graphic_source_updated_at": record.get("updated_at", now),
        "graphic_model": GEMINI_IMAGE_MODEL,
        "graphic_prompt_version": GRAPHIC_PROMPT_VERSION,
        "graphic_pending_version": int(record.get("graphic_version", 0) or 0) + 1,
    }
    updated = update_case_graphic_metadata(
        record["case_id"],
        updates,
        remove=["graphic_error_type", "graphic_error_message"],
    )
    put_case_markdown(updated)
    log_event(
        "case_graphic_scheduled",
        case_id=record["case_id"],
        due_at=due_at,
        delay_seconds=GRAPHIC_GENERATION_DELAY_SECONDS,
    )
    return updated


def process_pending_graphics() -> dict[str, Any]:
    if not GRAPHIC_GENERATION_ENABLED:
        return {
            "ok": True,
            "action": "graphic_maintenance",
            "processed_count": 0,
            "generated_count": 0,
            "failed_count": 0,
            "slack_text": "",
        }

    records = scan_due_graphic_cases(GRAPHIC_MAINTENANCE_BATCH_SIZE)
    generated_count = 0
    failed_count = 0
    processed_case_ids: list[str] = []
    for candidate in records:
        record = get_case_by_id(candidate["case_id"])
        if not record or not is_graphic_due(record):
            continue
        processed_case_ids.append(record["case_id"])
        updated = refresh_case_graphic(record)
        if updated.get("graphic_status") == "ready":
            generated_count += 1
        else:
            failed_count += 1

    log_event(
        "graphic_maintenance_completed",
        processed_count=len(processed_case_ids),
        generated_count=generated_count,
        failed_count=failed_count,
        case_ids=processed_case_ids,
    )
    return {
        "ok": failed_count == 0,
        "action": "graphic_maintenance",
        "processed_count": len(processed_case_ids),
        "generated_count": generated_count,
        "failed_count": failed_count,
        "slack_text": "",
    }


def process_knowledge_base_sync() -> dict[str, Any]:
    records = scan_active_cases()
    synced_count = 0
    for record in records:
        # title欠落などの不完全レコード（削除後のグラレコ書き込み等で生じ得る）は
        # 1件で同期全体を止めないようスキップして警告を残す
        if not record.get("case_id") or not record.get("title"):
            log_event("knowledge_base_sync_skipped_invalid_record", case_id=record.get("case_id"))
            continue
        put_case_markdown(record)
        synced_count += 1

    ingestion_result = start_knowledge_base_ingestion()
    log_event(
        "knowledge_base_sync_completed",
        synced_count=synced_count,
        ingestion_status=ingestion_result.get("ingestion_status"),
        ingestion_job_id=ingestion_result.get("ingestion_job_id"),
    )
    return {
        "ok": ingestion_result.get("ok", False),
        "action": "knowledge_base_sync",
        "synced_count": synced_count,
        "slack_text": "",
        **ingestion_result,
    }


def start_knowledge_base_ingestion() -> dict[str, Any]:
    if not KNOWLEDGE_BASE_ID or not KNOWLEDGE_BASE_DATA_SOURCE_ID:
        return {
            "ok": False,
            "ingestion_status": "disabled",
            "error": "knowledge_base_not_configured",
        }

    try:
        response = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=KNOWLEDGE_BASE_DATA_SOURCE_ID,
            clientToken=f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
            description="Scheduled ingestion for AI社内事例おしえて君 case Markdown.",
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", type(exc).__name__)
        if error_code in {"ConflictException", "ConflictError"}:
            log_event("knowledge_base_ingestion_already_running")
            return {
                "ok": True,
                "ingestion_status": "already_running",
            }
        log_event("knowledge_base_ingestion_start_failed", error_code=error_code)
        return {
            "ok": False,
            "ingestion_status": "failed_to_start",
            "error": error_code,
        }

    job = response.get("ingestionJob", {})
    return {
        "ok": True,
        "ingestion_job_id": job.get("ingestionJobId", ""),
        "ingestion_status": job.get("status", "STARTING"),
    }


def scan_due_graphic_cases(limit: int) -> list[dict[str, Any]]:
    # DynamoDB scanのLimitはFilterExpression適用前の読み取り件数を絞るため、
    # ここで渡すとハッシュキー順で先頭に来た候補だけに偏り、他の候補が
    # 何度スキャンされても選ばれない飢餓状態になる。まず全件をフィルタ条件で
    # 収集し、due_epoch昇順（登録・更新が早い順）にソートしてからlimit件を返す。
    records: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": Attr("graphic_status").eq("pending") & Attr("graphic_due_epoch").lte(epoch_seconds()),
    }
    while True:
        response = cases_table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            if item.get("status") != "archived" and item.get("case_id"):
                records.append(item)
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    records.sort(key=lambda record: int(record.get("graphic_due_epoch", 0)))
    return records[:limit]


def is_graphic_due(record: dict[str, Any]) -> bool:
    if record.get("graphic_status") != "pending":
        return False
    try:
        return int(record.get("graphic_due_epoch", 0)) <= epoch_seconds()
    except (TypeError, ValueError):
        return False


def put_case_markdown(record: dict[str, Any]) -> None:
    s3.put_object(
        Bucket=CASE_ASSETS_BUCKET_NAME,
        Key=markdown_key(record["case_id"]),
        ContentType="text/markdown; charset=utf-8",
        Body=to_markdown(record).encode("utf-8"),
    )


def delete_case_markdown_objects(case_id: str) -> None:
    for key in {markdown_key(case_id), legacy_markdown_key(case_id)}:
        try:
            s3.delete_object(Bucket=CASE_ASSETS_BUCKET_NAME, Key=key)
        except Exception as exc:
            log_event("case_markdown_delete_failed", case_id=case_id, key=key, error_type=type(exc).__name__)


def refresh_case_graphic(record: dict[str, Any]) -> dict[str, Any]:
    if not GRAPHIC_GENERATION_ENABLED:
        return record

    now = utc_now_iso()
    try:
        image_bytes, mime_type = generate_case_graphic(record)
        key = graphic_key(record["case_id"], mime_type)
        old_key = record.get("graphic_s3_key")
        s3.put_object(
            Bucket=CASE_ASSETS_BUCKET_NAME,
            Key=key,
            ContentType=mime_type,
            Body=image_bytes,
            Metadata={"case-id": record["case_id"], "model": GEMINI_IMAGE_MODEL},
        )
        updates = {
            "graphic_status": "ready",
            "graphic_s3_key": key,
            "graphic_mime_type": mime_type,
            "graphic_generated_at": now,
            "graphic_model": GEMINI_IMAGE_MODEL,
            "graphic_prompt_version": GRAPHIC_PROMPT_VERSION,
            "graphic_version": int(record.get("graphic_version", 0) or 0) + 1,
            "graphic_alt_text": build_graphic_alt_text(record),
        }
        if isinstance(old_key, str) and old_key and old_key != key:
            s3.delete_object(Bucket=CASE_ASSETS_BUCKET_NAME, Key=old_key)
        log_event("case_graphic_generated", case_id=record["case_id"], model=GEMINI_IMAGE_MODEL)
    except Exception as exc:
        error_message = sanitize_error_message(exc)
        updates = {
            "graphic_status": "failed",
            "graphic_generated_at": now,
            "graphic_model": GEMINI_IMAGE_MODEL,
            "graphic_error_type": type(exc).__name__,
            "graphic_error_message": error_message,
        }
        log_event(
            "case_graphic_generation_failed",
            case_id=record["case_id"],
            error_type=type(exc).__name__,
            error_message=error_message,
        )

    updated = update_case_graphic_metadata(
        record["case_id"],
        updates,
        remove=["graphic_due_at", "graphic_due_epoch", "graphic_requested_at", "graphic_pending_version"],
    )
    put_case_markdown(updated)
    return updated


def update_case_graphic_metadata(case_id: str, updates: dict[str, Any], remove: list[str] | None = None) -> dict[str, Any]:
    names: dict[str, str] = {}
    values: dict[str, Any] = {}
    set_parts: list[str] = []
    for index, (key, value) in enumerate(updates.items()):
        name_key = f"#graphic{index}"
        value_key = f":graphic{index}"
        names[name_key] = key
        values[value_key] = value
        set_parts.append(f"{name_key} = {value_key}")

    remove_parts: list[str] = []
    for index, key in enumerate(remove or [], start=len(updates)):
        name_key = f"#graphic{index}"
        names[name_key] = key
        remove_parts.append(name_key)

    expressions: list[str] = []
    if set_parts:
        expressions.append(f"SET {', '.join(set_parts)}")
    if remove_parts:
        expressions.append(f"REMOVE {', '.join(remove_parts)}")

    # 削除済みcase_idへの遅延書き込み（グラレコ生成等）が部分レコードを
    # 復活させないよう、既存レコードがある時だけ更新する
    response = cases_table.update_item(
        Key={"case_id": case_id},
        UpdateExpression=" ".join(expressions),
        ConditionExpression="attribute_exists(case_id)",
        ExpressionAttributeNames=names,
        **({"ExpressionAttributeValues": values} if values else {}),
        ReturnValues="ALL_NEW",
    )
    return response["Attributes"]


def generate_case_graphic(record: dict[str, Any]) -> tuple[bytes, str]:
    prompt = build_graphic_prompt(record)
    client = get_gemini_client()
    if not GOOGLE_GENAI_USE_VERTEXAI:
        response = client.interactions.create(
            model=GEMINI_IMAGE_MODEL,
            input=prompt,
            response_format={
                "type": "image",
                "mime_type": "image/jpeg",
                "aspect_ratio": "16:9",
                "image_size": GEMINI_IMAGE_SIZE,
            },
        )
    else:
        response = client.models.generate_content(
            model=GEMINI_IMAGE_MODEL,
            contents=prompt,
            config=get_genai_image_config(),
        )
    image = extract_gemini_image(response)
    if image:
        return image
    raise RuntimeError("gemini_image_generation_returned_no_image")


def get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client

    if GOOGLE_GENAI_USE_VERTEXAI:
        ensure_google_credentials_file()
        _gemini_client = genai.Client(
            vertexai=True,
            project=GOOGLE_CLOUD_PROJECT,
            location=GOOGLE_CLOUD_LOCATION,
        )
    else:
        _gemini_client = genai.Client(api_key=get_gemini_api_key())
    return _gemini_client


def get_genai_image_config() -> Any:
    from google.genai.types import GenerateContentConfig, ImageConfig, Modality

    # image_size(1K/2K/4K)は Gemini 3 系のみ対応。2.5系に渡すと INVALID_ARGUMENT になる
    if GEMINI_IMAGE_MODEL.startswith("gemini-3"):
        image_config = ImageConfig(aspect_ratio="16:9", image_size=GEMINI_IMAGE_SIZE)
    else:
        image_config = ImageConfig(aspect_ratio="16:9")
    return GenerateContentConfig(
        response_modalities=[Modality.TEXT, Modality.IMAGE],
        candidate_count=1,
        image_config=image_config,
    )


def ensure_google_credentials_file() -> None:
    global _google_credentials_path
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return
    if _google_credentials_path:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _google_credentials_path
        return
    if not GOOGLE_WIF_CREDENTIAL_CONFIG_PARAMETER_NAME:
        return

    response = ssm.get_parameter(Name=GOOGLE_WIF_CREDENTIAL_CONFIG_PARAMETER_NAME, WithDecryption=True)
    value = response.get("Parameter", {}).get("Value")
    if not value:
        raise RuntimeError("google_wif_credential_config_parameter_is_empty")
    json.loads(value)
    path = "/tmp/google-wif-credentials.json"
    with open(path, "w", encoding="utf-8") as credential_file:
        credential_file.write(value)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
    _google_credentials_path = path


def image_extension(mime_type: str) -> str:
    if mime_type == "image/jpeg":
        return "jpg"
    if mime_type == "image/webp":
        return "webp"
    return "png"


def get_gemini_api_key() -> str:
    global _gemini_api_key
    if _gemini_api_key:
        return _gemini_api_key
    response = ssm.get_parameter(Name=GEMINI_API_KEY_PARAMETER_NAME, WithDecryption=True)
    value = response.get("Parameter", {}).get("Value", "")
    if not value:
        raise RuntimeError("gemini_api_key_parameter_is_empty")
    _gemini_api_key = value
    return value


def get_tavily_api_key() -> str:
    global _tavily_api_key
    if _tavily_api_key:
        return _tavily_api_key
    response = ssm.get_parameter(Name=TAVILY_API_KEY_PARAMETER_NAME, WithDecryption=True)
    value = response.get("Parameter", {}).get("Value", "")
    if not value:
        raise RuntimeError("tavily_api_key_parameter_is_empty")
    _tavily_api_key = value
    return value


def extract_gemini_image(response: Any) -> tuple[bytes, str] | None:
    image = find_gemini_image(response, set())
    if image:
        return image
    return None


def find_gemini_image(value: Any, seen: set[int], depth: int = 0) -> tuple[bytes, str] | None:
    if value is None or depth > 12:
        return None
    if isinstance(value, (str, bytes, bytearray, int, float, bool)):
        return None

    value_id = id(value)
    if value_id in seen:
        return None
    seen.add(value_id)

    data = get_field(value, "data")
    mime_type = normalize_mime_type(get_field(value, "mime_type") or get_field(value, "mimeType") or get_field(value, "media_type"))
    if is_image_mime(mime_type) and isinstance(data, (str, bytes, bytearray)):
        return decode_image_data(data, mime_type)

    for field_name in (
        "output_image",
        "outputImage",
        "inline_data",
        "inlineData",
        "steps",
        "parts",
        "content",
        "candidates",
        "outputs",
        "output",
        "response",
    ):
        child = get_field(value, field_name)
        image = find_gemini_image(child, seen, depth + 1)
        if image:
            return image

    if isinstance(value, dict):
        for child in value.values():
            image = find_gemini_image(child, seen, depth + 1)
            if image:
                return image
    elif isinstance(value, (list, tuple)):
        for child in value:
            image = find_gemini_image(child, seen, depth + 1)
            if image:
                return image
    else:
        for dump_method in ("model_dump", "to_dict", "dict"):
            method = getattr(value, dump_method, None)
            if not callable(method):
                continue
            try:
                dumped = method()
            except Exception:
                continue
            image = find_gemini_image(dumped, seen, depth + 1)
            if image:
                return image

    return None


def get_field(value: Any, field_name: str) -> Any:
    if isinstance(value, dict):
        return value.get(field_name)
    return getattr(value, field_name, None)


def normalize_mime_type(value: Any) -> str:
    if isinstance(value, str) and value.startswith("image/"):
        return value
    return ""


def is_image_mime(value: str) -> bool:
    return value.startswith("image/")


def decode_image_data(data: str | bytes | bytearray, mime_type: str) -> tuple[bytes, str]:
    if isinstance(data, str):
        return base64.b64decode(data), mime_type
    if isinstance(data, bytearray):
        return bytes(data), mime_type
    return data, mime_type


def build_graphic_prompt(record: dict[str, Any]) -> str:
    tags = ", ".join(build_display_tags(record)[:8])
    tech = ", ".join(str(item) for item in (record.get("technologies") or [])[:6])
    return "\n".join(
        [
            "社内の事例共有チャンネルに投稿する、横長16:9のグラフィックレコーディング画像を1枚生成してください。",
            "",
            "全体スタイル:",
            "- オフホワイトの背景に、手描き風の線とやわらかい配色（青・緑・オレンジ・紫などのアクセントカラー）でまとめてください。",
            "- 最上部に大きな手描き風タイトルを置き、カラーマーカーで下線や蛍光ハイライトを付けてください。タイトル直下に事例の要点を1行のサブタイトルとして添えてください。",
            "- 紙面全体を5〜7個の角丸ボックスに区切り、各ボックスに①②③のような番号付き見出しを付けて、隙間なく情報を敷き詰めてください。",
            "- 各ボックスの中は「小さなアイコン＋短いフレーズ」の箇条書きを2〜4個入れ、チェックマーク・矢印・吹き出し・星などの手描き装飾で読み流せるようにしてください。",
            "- 最下部に横長のまとめバナーを置き、事例の価値を1文で言い切ってください。",
            "",
            "紙面構成（事例情報に内容がある項目だけボックス化し、空の項目は省略して他のボックスを充実させてください）:",
            "① 背景・課題: どんな困りごとがあったか。",
            "② 取り組みの流れ: アイコンと矢印でつないだ3〜5ステップの横向きフロー。",
            "③ 工夫・ポイント: 技術面や進め方の工夫をチェックリスト形式で。",
            "④ 効果・成果: 得られた効果を強調色で目立たせる。",
            "⑤ 技術・キーワード: 使った技術やタグを小さなラベル群で。",
            "下部の隅に、聞き先・関連部署の小さなメモ欄を添えてください。",
            "",
            "守ること:",
            "- ボックスの番号と見出しは重複させず、同じ内容のボックスを2回描かないでください。",
            "- 文字はすべて正確で読みやすい日本語にしてください。長文を書き写さず、短い見出しとキーフレーズに分解してください。",
            "- 事例情報に書かれていない数値・固有名詞・効果を創作しないでください。情報を分解・言い換えして密度を出すのは歓迎です。",
            "- 写真風・企業ロゴ風・実在人物の似顔絵・メールアドレス・電話番号・顧客担当者名・秘密情報は描かないでください。デフォルメされた匿名の人物イラストは使ってかまいません。",
            "",
            "事例情報:",
            f"タイトル: {record.get('title', '')}",
            f"概要: {record.get('summary', '')}",
            f"対象: {record.get('project_or_customer', '')}",
            f"業界: {record.get('customer_industry', '')}",
            f"部署: {record.get('department', '') or record.get('owner_department', '')}",
            f"聞き先: {record.get('contact_hint', '') or record.get('owner_name', '')}",
            f"課題: {record.get('problem', '')}",
            f"取り組み: {record.get('approach', '')}",
            f"効果: {record.get('impact', '')}",
            f"技術: {tech}",
            f"タグ: {tags}",
        ]
    )


def build_graphic_alt_text(record: dict[str, Any]) -> str:
    return truncate(f"{record.get('title', '社内事例')} のグラフィックレコーディング画像", 120)


def with_case_graphic(result: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    graphic = case_graphic_payload(record)
    if not graphic:
        return result
    return {
        **result,
        "graphic_s3_key": graphic["s3_key"],
        "graphic_mime_type": graphic["mime_type"],
        "graphic_alt_text": graphic["alt_text"],
        "graphics": [graphic],
    }


def case_graphic_payload(record: dict[str, Any]) -> dict[str, Any] | None:
    if record.get("graphic_status") != "ready" or not record.get("graphic_s3_key"):
        return None
    return {
        "case_id": record["case_id"],
        "title": record.get("title", "社内事例グラレコ"),
        "s3_key": record["graphic_s3_key"],
        "mime_type": record.get("graphic_mime_type", "image/png"),
        "alt_text": record.get("graphic_alt_text") or build_graphic_alt_text(record),
    }


def compact_graphics(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    graphics: list[dict[str, Any]] = []
    for record in records:
        graphic = case_graphic_payload(record)
        if graphic:
            graphics.append(graphic)
    return graphics


def to_markdown(record: dict[str, Any]) -> str:
    keywords = "\n".join(f"- {keyword}" for keyword in record.get("keywords", []))
    attributes = format_markdown_attributes(record)
    return "\n".join(
        [
            f"# {record['title']}",
            "",
            f"- case_id: {record['case_id']}",
            f"- status: {record['status']}",
            f"- visibility: {record['visibility']}",
            f"- redaction_status: {record['redaction_status']}",
            f"- source: {record.get('source_message_url', '')}",
            f"- created_at: {record['created_at']}",
            f"- updated_at: {record['updated_at']}",
            "",
            "## Summary",
            "",
            record["summary"],
            "",
            "## Attributes",
            "",
            attributes,
            "",
            "## Keywords",
            "",
            keywords,
        ]
    )


def format_markdown_attributes(record: dict[str, Any]) -> str:
    rows = []
    for key in [
        "project_or_customer",
        "customer_industry",
        "department",
        "contact_hint",
        "problem",
        "approach",
        "impact",
    ]:
        if record.get(key):
            rows.append(f"- {key}: {record[key]}")
    if record.get("owner_department"):
        rows.append(f"- owner_department: {record['owner_department']}")
    if record.get("owner_name"):
        rows.append(f"- owner_name: {record['owner_name']}")
    if record.get("case_type"):
        rows.append(f"- case_type: {record['case_type']}")
    if record.get("team"):
        rows.append(f"- team: {record['team']}")
    for key in ["technologies", "tags"]:
        values = record.get(key)
        if isinstance(values, list) and values:
            rows.append(f"- {key}: {', '.join(str(value) for value in values)}")
    if record.get("graphic_status"):
        rows.append(f"- graphic_status: {record['graphic_status']}")
    if record.get("graphic_s3_key"):
        rows.append(f"- graphic_s3_key: {record['graphic_s3_key']}")
    source_urls = record.get("external_source_urls")
    if isinstance(source_urls, list) and source_urls:
        rows.append(f"- external_source_urls: {', '.join(str(url) for url in source_urls[:5])}")
    return "\n".join(rows) if rows else "- 未設定"


def parse_json_from_text(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


def result_to_text(result: Any) -> str:
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            texts = [item.get("text", "") for item in content if isinstance(item, dict)]
            if any(texts):
                return "\n".join(texts)
        if isinstance(content, str):
            return content
    return str(result)


def build_case_id(now_iso: str) -> str:
    stamp = re.sub(r"[-:.TZ+]", "", now_iso)[:14]
    return f"case-{stamp}-{str(uuid.uuid4())[:8]}"


def build_thread_key(channel: str, thread_ts: str) -> str:
    return f"{channel}:{thread_ts}"


def build_slack_message_url(channel: str, ts: str) -> str:
    if not SLACK_WORKSPACE_URL or not channel or not ts:
        return ""
    return f"{SLACK_WORKSPACE_URL}/archives/{channel}/p{ts.replace('.', '')}"


def markdown_key(case_id: str) -> str:
    return f"{CASE_KNOWLEDGE_SOURCE_PREFIX.rstrip('/')}/{case_id}.md"


def legacy_markdown_key(case_id: str) -> str:
    return f"cases/{case_id}.md"


def graphic_key(case_id: str, mime_type: str = "image/png") -> str:
    extension = "jpg" if mime_type == "image/jpeg" else "png"
    return f"cases/{case_id}/graphic.{extension}"


def truncate(text: str, max_length: int) -> str:
    return text if len(text) <= max_length else f"{text[: max_length - 1]}…"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_iso_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def epoch_seconds() -> int:
    return int(datetime.now(timezone.utc).timestamp())


if __name__ == "__main__":
    app.run()
