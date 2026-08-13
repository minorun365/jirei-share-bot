import { createHash, createHmac, randomUUID, timingSafeEqual } from "node:crypto";
import type { APIGatewayProxyStructuredResultV2, APIGatewayProxyEventV2, SQSEvent, SQSRecord } from "aws-lambda";
import { BedrockAgentCoreClient, InvokeAgentRuntimeCommand, StopRuntimeSessionCommand } from "@aws-sdk/client-bedrock-agentcore";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { InvokeCommand, LambdaClient } from "@aws-sdk/client-lambda";
import { GetObjectCommand, S3Client } from "@aws-sdk/client-s3";
import { SendMessageCommand, SQSClient } from "@aws-sdk/client-sqs";
import {
  DynamoDBDocumentClient,
  GetCommand,
  PutCommand,
  UpdateCommand
} from "@aws-sdk/lib-dynamodb";
import { GetParameterCommand, SSMClient } from "@aws-sdk/client-ssm";

const ssm = new SSMClient({});
const agentCore = new BedrockAgentCoreClient({});
const lambda = new LambdaClient({});
const s3 = new S3Client({});
const sqs = new SQSClient({});
const dynamodb = DynamoDBDocumentClient.from(new DynamoDBClient({}), {
  marshallOptions: { removeUndefinedValues: true }
});

const THREAD_STATE_TTL_SECONDS = 90 * 24 * 60 * 60;
const EVENT_PROCESSING_TTL_SECONDS = 24 * 60 * 60;
const MAX_QUEUE_RECEIVE_COUNT = 3;

let cachedSigningSecret: string | undefined;
let cachedBotToken: string | undefined;

interface SlackUrlVerificationPayload {
  readonly type: "url_verification";
  readonly challenge: string;
}

interface SlackEventCallbackPayload {
  readonly type: "event_callback";
  readonly event_id: string;
  readonly event: {
    readonly type: string;
    readonly channel?: string;
    readonly user?: string;
    readonly text?: string;
    readonly thread_ts?: string;
    readonly ts?: string;
  };
}

type SlackPayload = SlackUrlVerificationPayload | SlackEventCallbackPayload;

interface ProcessSlackEventInvocation {
  readonly mode: "process_slack_event";
  readonly payload: SlackEventCallbackPayload;
}

interface EventProcessingState {
  readonly event_id: string;
  readonly status?: "processing" | "agent_completed" | "delivered" | "failed_notified";
  readonly agent_response?: unknown;
}

interface SlackQueueMessage {
  readonly body: string;
  readonly messageGroupId: string;
  readonly messageDeduplicationId: string;
}

interface DailyShareEvent {
  readonly mode: "daily_share";
  readonly channelId?: string;
}

interface GraphicMaintenanceEvent {
  readonly mode: "graphic_maintenance";
}

interface KnowledgeBaseSyncEvent {
  readonly mode: "knowledge_base_sync";
}

type HandlerEvent =
  | APIGatewayProxyEventV2
  | SQSEvent
  | DailyShareEvent
  | GraphicMaintenanceEvent
  | KnowledgeBaseSyncEvent
  | ProcessSlackEventInvocation;

interface ScheduledInvocationResult {
  readonly ok: boolean;
  readonly shared?: boolean;
  readonly caseId?: string;
  readonly syncedCount?: number;
  readonly ingestionJobId?: string;
  readonly ingestionStatus?: string;
  readonly processedCount?: number;
  readonly generatedCount?: number;
  readonly failedCount?: number;
}

interface ChatPostMessageResult {
  readonly ts?: string;
  readonly channel?: string;
}

interface SlackUploadUrlResult {
  readonly upload_url?: string;
  readonly file_id?: string;
}

interface SlackCompleteUploadResult {
  readonly file?: SlackFile;
  readonly files?: readonly SlackFile[];
}

interface SlackFile {
  readonly shares?: SlackFileShares;
}

type SlackFileShares = Record<string, Record<string, readonly SlackFileShare[] | undefined> | undefined>;

interface SlackFileShare {
  readonly ts?: string;
}

interface ThreadContextMessage {
  readonly ts?: string;
  readonly user?: string;
  readonly bot_id?: string;
  readonly text: string;
  readonly is_current?: boolean;
}

interface CaseAgentRequest {
  readonly mode: "slack_app_mention" | "daily_share" | "graphic_maintenance" | "knowledge_base_sync";
  readonly text?: string;
  readonly channel_id?: string;
  readonly message_ts?: string;
  readonly thread_ts?: string;
  readonly user_id?: string;
  readonly target_channel_id?: string;
  readonly thread_messages?: readonly ThreadContextMessage[];
}

interface CaseAgentResponse {
  readonly ok?: boolean;
  readonly action?: string;
  readonly slack_text?: string;
  readonly case_id?: string;
  readonly post_to_channel?: boolean;
  readonly graphic_s3_key?: string;
  readonly graphic_mime_type?: string;
  readonly graphic_alt_text?: string;
  readonly graphics?: readonly CaseGraphic[];
  readonly deleted_count?: number;
  readonly processed_count?: number;
  readonly generated_count?: number;
  readonly failed_count?: number;
  readonly synced_count?: number;
  readonly ingestion_job_id?: string;
  readonly ingestion_status?: string;
  readonly error?: string;
  readonly agent?: CaseAgentMetadata;
}

interface CaseGraphic {
  readonly case_id?: string;
  readonly title?: string;
  readonly s3_key: string;
  readonly mime_type?: string;
  readonly alt_text?: string;
}

interface CaseAgentMetadata {
  readonly runtime?: string;
  readonly framework?: string;
  readonly model_id?: string;
  readonly tool_calls?: readonly string[];
}

export async function handler(
  event: HandlerEvent
): Promise<APIGatewayProxyStructuredResultV2 | ScheduledInvocationResult | void> {
  if (isDailyShareEvent(event)) {
    return handleDailyShare(event);
  }
  if (isGraphicMaintenanceEvent(event)) {
    return handleGraphicMaintenance();
  }
  if (isKnowledgeBaseSyncEvent(event)) {
    return handleKnowledgeBaseSync();
  }
  if (isProcessSlackEventInvocation(event)) {
    await processAppMention(event.payload);
    return { ok: true };
  }
  if (isSqsEvent(event)) {
    for (const record of event.Records) {
      await processQueuedSlackEvent(record);
    }
    return;
  }

  const rawBody = getRawBody(event);
  const signingSecret = await getSigningSecret();

  if (!verifySlackSignature(rawBody, event.headers, signingSecret)) {
    return { statusCode: 401, body: "invalid signature" };
  }

  const payload = JSON.parse(rawBody) as SlackPayload;

  if (payload.type === "url_verification") {
    return json(200, { challenge: payload.challenge });
  }

  if (payload.type !== "event_callback" || !isProcessableSlackEvent(payload.event)) {
    return json(200, { ok: true, ignored: true });
  }

  // Slack の3秒ACK制約を守り、SQSへの永続化が成功してから200を返す
  await dispatchSlackEventProcessing(payload);

  return json(200, {
    ok: true,
    accepted: true,
    eventId: payload.event_id,
    threadTs: payload.event.thread_ts ?? payload.event.ts
  });
}

async function dispatchSlackEventProcessing(payload: SlackEventCallbackPayload): Promise<void> {
  const message = buildSlackQueueMessage(payload);
  await sqs.send(
    new SendMessageCommand({
      QueueUrl: requiredEnv("SLACK_EVENT_QUEUE_URL"),
      MessageBody: message.body,
      MessageGroupId: message.messageGroupId,
      MessageDeduplicationId: message.messageDeduplicationId
    })
  );
}

export function buildSlackQueueMessage(payload: SlackEventCallbackPayload): SlackQueueMessage {
  const invocation: ProcessSlackEventInvocation = { mode: "process_slack_event", payload };
  const channel = payload.event.channel ?? "unknown-channel";
  const threadTs = payload.event.thread_ts ?? payload.event.ts ?? payload.event_id;
  return {
    body: JSON.stringify(invocation),
    messageGroupId: boundedQueueIdentifier(`${channel}:${threadTs}`),
    messageDeduplicationId: boundedQueueIdentifier(payload.event_id)
  };
}

function boundedQueueIdentifier(value: string): string {
  return value.length <= 128 ? value : createHash("sha256").update(value).digest("hex");
}

// SQS切り替えの観測期間中だけ残すロールバック用の旧自己invoke経路。
// 安定確認後にIAM権限・configureAsyncInvokeと一緒に削除する
async function dispatchSlackEventProcessingBySelfInvoke(payload: SlackEventCallbackPayload): Promise<void> {
  const invocation: ProcessSlackEventInvocation = { mode: "process_slack_event", payload };
  await lambda.send(
    new InvokeCommand({
      FunctionName: requiredEnv("AWS_LAMBDA_FUNCTION_NAME"),
      InvocationType: "Event",
      Payload: Buffer.from(JSON.stringify(invocation), "utf8")
    })
  );
}

export function verifySlackSignature(
  rawBody: string,
  headers: Record<string, string | undefined>,
  signingSecret: string,
  nowSeconds = Math.floor(Date.now() / 1000)
): boolean {
  const timestamp = headers["x-slack-request-timestamp"] ?? headers["X-Slack-Request-Timestamp"];
  const signature = headers["x-slack-signature"] ?? headers["X-Slack-Signature"];

  if (!timestamp || !signature) {
    return false;
  }

  const timestampSeconds = Number(timestamp);
  if (!Number.isFinite(timestampSeconds) || Math.abs(nowSeconds - timestampSeconds) > 60 * 5) {
    return false;
  }

  const base = `v0:${timestamp}:${rawBody}`;
  const expected = `v0=${createHmac("sha256", signingSecret).update(base).digest("hex")}`;
  return safeCompare(expected, signature);
}

function safeCompare(left: string, right: string): boolean {
  const leftBuffer = Buffer.from(left);
  const rightBuffer = Buffer.from(right);
  if (leftBuffer.length !== rightBuffer.length) {
    return false;
  }
  return timingSafeEqual(leftBuffer, rightBuffer);
}

function getRawBody(event: APIGatewayProxyEventV2): string {
  if (!event.body) {
    return "";
  }
  return event.isBase64Encoded ? Buffer.from(event.body, "base64").toString("utf8") : event.body;
}

// パブリック ch の app_mention（メンション必須）だけを処理する。
// DM・添付ファイル取り込みは 2026-07-08 に不採用と決定（資料は Bot の本人OAuthから
// アクセスできる Google Drive URL で受け渡す運用）。Bot の DM 内メンションも対象外にする
export function isProcessableSlackEvent(event: SlackEventCallbackPayload["event"]): boolean {
  return event.type === "app_mention" && !(event.channel ?? "").startsWith("D");
}

async function processQueuedSlackEvent(record: SQSRecord): Promise<void> {
  const invocation = parseSlackQueueMessage(record.body);
  const payload = invocation.payload;
  const event = payload.event;
  const channel = event.channel;
  const messageTs = event.ts;
  const threadTs = event.thread_ts ?? event.ts;
  const receiveCount = Number(record.attributes.ApproximateReceiveCount || "1");

  if (!channel || !messageTs || !threadTs) {
    throw new Error(`Invalid queued Slack event: ${payload.event_id}`);
  }

  const claim = await claimQueuedEvent(payload.event_id, receiveCount);
  if (claim.kind === "skip") {
    console.info("slack_event_queue_duplicate_skipped", {
      eventId: payload.event_id,
      reason: claim.reason
    });
    return;
  }

  try {
    if (!(await isReadablePublicChannel(channel))) {
      await postThreadReply(
        channel,
        threadTs,
        "ごめんなさい、このチャンネルの種別ではまだ利用できません。Botを招待したパブリックチャンネルでメンションしてください。"
      );
      await markQueuedEventDelivered(payload.event_id);
      return;
    }

    let response = claim.cachedResponse;
    if (!response) {
      response = await invokeCaseAgent(await buildAppMentionAgentRequest(event));
      logAgentResponse("slack_app_mention", response);
      await storeQueuedAgentResponse(payload.event_id, response);
    }

    await postAgentResponseToThread(channel, threadTs, response);
    await markQueuedEventDelivered(payload.event_id);
  } catch (error) {
    console.error("slack_app_mention_queue_processing_failed", {
      eventId: payload.event_id,
      receiveCount,
      error: toSafeError(error)
    });
    if (receiveCount >= MAX_QUEUE_RECEIVE_COUNT) {
      await tryPostThreadReply(channel, threadTs, buildProcessingErrorReply(error, payload.event_id));
      await markQueuedEventFailed(payload.event_id);
    }
    throw error;
  }
}

export function parseSlackQueueMessage(body: string): ProcessSlackEventInvocation {
  const parsed = JSON.parse(body) as unknown;
  if (
    !isRecord(parsed) ||
    parsed.mode !== "process_slack_event" ||
    !isRecord(parsed.payload) ||
    parsed.payload.type !== "event_callback" ||
    typeof parsed.payload.event_id !== "string" ||
    !isRecord(parsed.payload.event) ||
    typeof parsed.payload.event.type !== "string"
  ) {
    throw new Error("Invalid Slack event queue message");
  }
  return parsed as unknown as ProcessSlackEventInvocation;
}

async function claimQueuedEvent(
  eventId: string,
  receiveCount: number
): Promise<
  | { readonly kind: "process"; readonly cachedResponse?: CaseAgentResponse }
  | { readonly kind: "skip"; readonly reason: "delivered" | "legacy_event" }
> {
  let state = await getEventProcessingState(eventId);
  if (!state) {
    try {
      await dynamodb.send(
        new PutCommand({
          TableName: requiredEnv("EVENT_DEDUPE_TABLE_NAME"),
          Item: {
            event_id: eventId,
            status: "processing",
            receive_count: receiveCount,
            updated_at: new Date().toISOString(),
            expires_at: epochSeconds() + EVENT_PROCESSING_TTL_SECONDS
          },
          ConditionExpression: "attribute_not_exists(event_id)"
        })
      );
      return { kind: "process" };
    } catch (error) {
      if (!(error instanceof Error) || error.name !== "ConditionalCheckFailedException") {
        throw error;
      }
      state = await getEventProcessingState(eventId);
    }
  }

  if (!state) {
    throw new Error(`Event processing state disappeared: ${eventId}`);
  }
  if (!state.status) {
    return { kind: "skip", reason: "legacy_event" };
  }
  if (state.status === "delivered") {
    return { kind: "skip", reason: "delivered" };
  }
  if (state.status === "failed_notified") {
    throw new Error(`Event is awaiting DLQ handling: ${eventId}`);
  }

  await dynamodb.send(
    new UpdateCommand({
      TableName: requiredEnv("EVENT_DEDUPE_TABLE_NAME"),
      Key: { event_id: eventId },
      UpdateExpression: "SET receive_count = :receiveCount, updated_at = :updatedAt, expires_at = :expiresAt",
      ExpressionAttributeValues: {
        ":receiveCount": receiveCount,
        ":updatedAt": new Date().toISOString(),
        ":expiresAt": epochSeconds() + EVENT_PROCESSING_TTL_SECONDS
      }
    })
  );

  if (state.status === "agent_completed") {
    if (!isRecord(state.agent_response)) {
      throw new Error(`Stored AgentCore response is invalid: ${eventId}`);
    }
    return { kind: "process", cachedResponse: normalizeAgentResponse(state.agent_response) };
  }
  return { kind: "process" };
}

async function getEventProcessingState(eventId: string): Promise<EventProcessingState | undefined> {
  const result = await dynamodb.send(
    new GetCommand({
      TableName: requiredEnv("EVENT_DEDUPE_TABLE_NAME"),
      Key: { event_id: eventId },
      ConsistentRead: true
    })
  );
  return result.Item as EventProcessingState | undefined;
}

async function storeQueuedAgentResponse(eventId: string, response: CaseAgentResponse): Promise<void> {
  await updateQueuedEventState(eventId, "agent_completed", response);
}

async function markQueuedEventDelivered(eventId: string): Promise<void> {
  await updateQueuedEventState(eventId, "delivered");
}

async function markQueuedEventFailed(eventId: string): Promise<void> {
  await updateQueuedEventState(eventId, "failed_notified");
}

async function updateQueuedEventState(
  eventId: string,
  status: NonNullable<EventProcessingState["status"]>,
  response?: CaseAgentResponse
): Promise<void> {
  await dynamodb.send(
    new UpdateCommand({
      TableName: requiredEnv("EVENT_DEDUPE_TABLE_NAME"),
      Key: { event_id: eventId },
      UpdateExpression: [
        "SET #status = :status",
        "updated_at = :updatedAt",
        "expires_at = :expiresAt",
        ...(response ? ["agent_response = :agentResponse"] : [])
      ].join(", "),
      ExpressionAttributeNames: { "#status": "status" },
      ExpressionAttributeValues: {
        ":status": status,
        ":updatedAt": new Date().toISOString(),
        ":expiresAt": epochSeconds() + EVENT_PROCESSING_TTL_SECONDS,
        ...(response ? { ":agentResponse": response } : {})
      },
      ConditionExpression: "attribute_exists(event_id)"
    })
  );
}

async function processAppMention(payload: SlackEventCallbackPayload): Promise<void> {
  const event = payload.event;
  const channel = event.channel;
  const messageTs = event.ts;
  const threadTs = event.thread_ts ?? event.ts;

  if (!channel || !messageTs || !threadTs) {
    return;
  }

  // パブリック ch 以外（private ch / 未対応の会話種別）は運用ポリシー上 fail-closed でお断りする
  if (!(await isReadablePublicChannel(channel))) {
    await tryPostThreadReply(
      channel,
      threadTs,
      "ごめんなさい、このチャンネルの種別ではまだ利用できません。Botを招待したパブリックチャンネルでメンションしてください。"
    );
    return;
  }

  try {
    const response = await invokeCaseAgent(await buildAppMentionAgentRequest(event));
    logAgentResponse("slack_app_mention", response);
    await postAgentResponseToThread(channel, threadTs, response);
  } catch (error) {
    console.error("slack_app_mention_processing_failed", {
      eventId: payload.event_id,
      error: toSafeError(error)
    });
    await tryPostThreadReply(channel, threadTs, buildProcessingErrorReply(error, payload.event_id));
  }
}

// 利用者向けのエラー返信。何が起きて誰が対応するのかが伝わる文面にする
// （本文・トークン等の入力内容はログに残していない）
function buildProcessingErrorReply(error: unknown, eventId: string | undefined): string {
  const safeError = toSafeError(error);
  const isRuntimeError = safeError.name === "RuntimeClientError" || safeError.message.includes("(500)");
  const cause = isRuntimeError
    ? "AI処理エンジン（AgentCore Runtime）の内部でエラーが発生しました"
    : `処理の途中で想定外のエラーが発生しました（種別: ${safeError.name}）`;
  const lines = [
    `ごめんなさい、${cause}:pray:`,
    "みなさんの操作や依頼内容の問題ではなく、Bot側の不具合です。エラー詳細は開発者向けのログに記録済みなので、運用担当者が調査します。",
    "急ぎの場合は、このスレッドのURLを添えて開発者に連絡してください。修正後に同じ内容でもう一度メンションしてもらえれば再処理できます。",
    ...(eventId ? [`（調査用ID: ${eventId}）`] : [])
  ];
  return lines.join("\n");
}

async function handleDailyShare(
  event: DailyShareEvent
): Promise<ScheduledInvocationResult> {
  const channelId = event.channelId ?? requiredEnv("TARGET_CHANNEL_ID");

  const response = await invokeCaseAgent({
    mode: "daily_share",
    target_channel_id: channelId
  });
  logAgentResponse("daily_share", response);
  if (!response.post_to_channel || !response.slack_text) {
    return { ok: response.ok !== false, shared: false };
  }

  const message = await postDailyShare(channelId, response);
  if (response.case_id) {
    await recordDailyShare(response.case_id, channelId, message.ts);
  }
  return { ok: response.ok !== false, shared: true, ...(response.case_id ? { caseId: response.case_id } : {}) };
}

async function handleGraphicMaintenance(): Promise<ScheduledInvocationResult> {
  const response = await invokeCaseAgent({ mode: "graphic_maintenance" });
  logAgentResponse("graphic_maintenance", response);
  return {
    ok: response.ok !== false,
    ...(typeof response.processed_count === "number" ? { processedCount: response.processed_count } : {}),
    ...(typeof response.generated_count === "number" ? { generatedCount: response.generated_count } : {}),
    ...(typeof response.failed_count === "number" ? { failedCount: response.failed_count } : {})
  };
}

async function handleKnowledgeBaseSync(): Promise<ScheduledInvocationResult> {
  const response = await invokeCaseAgent({ mode: "knowledge_base_sync" });
  logAgentResponse("knowledge_base_sync", response);
  return {
    ok: response.ok !== false,
    ...(typeof response.synced_count === "number" ? { syncedCount: response.synced_count } : {}),
    ...(typeof response.ingestion_job_id === "string" ? { ingestionJobId: response.ingestion_job_id } : {}),
    ...(typeof response.ingestion_status === "string" ? { ingestionStatus: response.ingestion_status } : {})
  };
}

async function buildAppMentionAgentRequest(event: SlackEventCallbackPayload["event"]): Promise<CaseAgentRequest> {
  const channelId = event.channel;
  const messageTs = event.ts;
  const threadTs = event.thread_ts ?? event.ts;
  const threadMessages =
    channelId && messageTs && threadTs && event.thread_ts
      ? await fetchThreadContext(channelId, threadTs, messageTs)
      : undefined;

  return {
    mode: "slack_app_mention",
    text: event.text ?? "",
    ...(channelId ? { channel_id: channelId } : {}),
    ...(messageTs ? { message_ts: messageTs } : {}),
    ...(threadTs ? { thread_ts: threadTs } : {}),
    ...(event.user ? { user_id: event.user } : {}),
    ...(channelId ? { target_channel_id: channelId } : {}),
    ...(threadMessages?.length ? { thread_messages: threadMessages } : {})
  };
}

const channelReadabilityCache = new Map<string, boolean>();

async function isReadablePublicChannel(channel: string): Promise<boolean> {
  const cached = channelReadabilityCache.get(channel);
  if (cached !== undefined) {
    return cached;
  }

  // channels:read scope を持たないため conversations.info は使えない。
  // 代わりに channels:history で 1 件だけ履歴を読めるか probe する。
  // private ch は groups:history 未付与で必ず失敗するので fail-closed になる。
  try {
    await callSlackApi("conversations.history", { channel, limit: 1 });
    channelReadabilityCache.set(channel, true);
    return true;
  } catch (error) {
    console.info("slack_channel_probe_rejected", { channel, error: toSafeError(error) });
    channelReadabilityCache.set(channel, false);
    return false;
  }
}

async function fetchThreadContext(channel: string, threadTs: string, currentMessageTs: string): Promise<ThreadContextMessage[]> {
  try {
    const result = await callSlackApi<{ readonly messages?: readonly Record<string, unknown>[] }>("conversations.replies", {
      channel,
      ts: threadTs,
      limit: 12
    });
    return (result.messages ?? [])
      .map((message) => ({
        ...(typeof message.ts === "string" ? { ts: message.ts } : {}),
        ...(typeof message.user === "string" ? { user: message.user } : {}),
        ...(typeof message.bot_id === "string" ? { bot_id: message.bot_id } : {}),
        text: truncateSlackText(typeof message.text === "string" ? message.text : ""),
        ...(message.ts === currentMessageTs ? { is_current: true } : {})
      }))
      .filter((message) => message.text.length > 0);
  } catch (error) {
    console.error("slack_thread_context_fetch_failed", { error: toSafeError(error) });
    return [];
  }
}

async function invokeCaseAgent(request: CaseAgentRequest): Promise<CaseAgentResponse> {
  const agentRuntimeArn = requiredEnv("AGENT_RUNTIME_ARN");
  const runtimeSessionId = buildAgentRuntimeSessionId(request);
  const response = await agentCore.send(
    new InvokeAgentRuntimeCommand({
      agentRuntimeArn,
      runtimeSessionId,
      contentType: "application/json",
      accept: "application/json",
      payload: Buffer.from(JSON.stringify(request), "utf8")
    })
  );
  const rawResponse = response.response ? await response.response.transformToString() : "";

  // 非対話（daily_share / graphic_maintenance / knowledge_base_sync）は会話継続が不要。
  // 応答を読み切った直後にセッションを明示終了し、アイドル待ち中のメモリ従量課金
  // （AgentCore Runtime コストの主因）を丸ごと止める。対話（slack_app_mention）は
  // 同一スレッドの追撃を温存するため終了せず、Runtime 側の idle タイムアウトに委ねる。
  if (request.mode !== "slack_app_mention") {
    await stopAgentRuntimeSession(agentRuntimeArn, runtimeSessionId);
  }

  return parseAgentResponse(rawResponse);
}

// セッションの明示終了はコスト最適化のベストエフォート。既に終了済み
// （ResourceNotFoundException）や一時的な失敗は握りつぶし、本処理の応答返却を止めない。
// stop に失敗しても最短60秒のidleタイムアウトでセッションを終了する。
async function stopAgentRuntimeSession(agentRuntimeArn: string, runtimeSessionId: string): Promise<void> {
  try {
    await agentCore.send(
      new StopRuntimeSessionCommand({
        agentRuntimeArn,
        runtimeSessionId
      })
    );
  } catch (error) {
    console.warn("agentcore_stop_session_failed", { runtimeSessionId, error: toSafeError(error) });
  }
}

function buildAgentRuntimeSessionId(request: CaseAgentRequest): string {
  // 定期実行系は毎回同じセッションIDだとAgentCoreセッションが旧バージョンの
  // コンテナに張り付き続け、デプロイが反映されないため毎回新セッションにする
  if (request.mode !== "slack_app_mention") {
    return `jirei-share-bot-${randomUUID()}`;
  }
  const stableKey = [
    request.mode,
    request.channel_id ?? request.target_channel_id ?? "channel",
    request.thread_ts ?? request.message_ts ?? "daily"
  ].join(":");
  const hash = createHash("sha256").update(stableKey).digest("hex");
  return `jirei-share-bot-${hash}`;
}

function parseAgentResponse(rawResponse: string): CaseAgentResponse {
  const parsed = parseJsonObject(rawResponse);
  if (parsed) {
    return normalizeAgentResponse(parsed);
  }

  const ssePayloads = rawResponse
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.startsWith("data:"))
    .map((line) => parseJsonObject(line.slice("data:".length).trim()))
    .filter((value): value is Record<string, unknown> => value !== undefined);

  const lastPayload = ssePayloads.at(-1);
  if (lastPayload) {
    return normalizeAgentResponse(lastPayload);
  }

  return {
    ok: false,
    action: "parse_error",
    slack_text: "AgentCore Runtime からの応答を読み取れませんでした。ログを確認します。"
  };
}

function normalizeAgentResponse(value: Record<string, unknown>): CaseAgentResponse {
  if (isRecord(value.result)) {
    return normalizeAgentResponse(value.result);
  }
  if (typeof value.result === "string") {
    const nested = parseJsonObject(value.result);
    if (nested) {
      return normalizeAgentResponse(nested);
    }
  }

  return {
    ...(typeof value.ok === "boolean" ? { ok: value.ok } : {}),
    ...(typeof value.action === "string" ? { action: value.action } : {}),
    ...(typeof value.slack_text === "string" ? { slack_text: value.slack_text } : {}),
    ...(typeof value.case_id === "string" ? { case_id: value.case_id } : {}),
    ...(typeof value.post_to_channel === "boolean" ? { post_to_channel: value.post_to_channel } : {}),
    ...(typeof value.graphic_s3_key === "string" ? { graphic_s3_key: value.graphic_s3_key } : {}),
    ...(typeof value.graphic_mime_type === "string" ? { graphic_mime_type: value.graphic_mime_type } : {}),
    ...(typeof value.graphic_alt_text === "string" ? { graphic_alt_text: value.graphic_alt_text } : {}),
    ...(Array.isArray(value.graphics) ? { graphics: value.graphics.map(normalizeCaseGraphic).filter(isCaseGraphic) } : {}),
    ...(typeof value.deleted_count === "number" ? { deleted_count: value.deleted_count } : {}),
    ...(typeof value.processed_count === "number" ? { processed_count: value.processed_count } : {}),
    ...(typeof value.generated_count === "number" ? { generated_count: value.generated_count } : {}),
    ...(typeof value.failed_count === "number" ? { failed_count: value.failed_count } : {}),
    ...(typeof value.synced_count === "number" ? { synced_count: value.synced_count } : {}),
    ...(typeof value.ingestion_job_id === "string" ? { ingestion_job_id: value.ingestion_job_id } : {}),
    ...(typeof value.ingestion_status === "string" ? { ingestion_status: value.ingestion_status } : {}),
    ...(typeof value.error === "string" ? { error: value.error } : {}),
    ...(isRecord(value.agent) ? { agent: normalizeAgentMetadata(value.agent) } : {})
  };
}

function normalizeCaseGraphic(value: unknown): CaseGraphic | undefined {
  if (!isRecord(value) || typeof value.s3_key !== "string") {
    return undefined;
  }
  return {
    ...(typeof value.case_id === "string" ? { case_id: value.case_id } : {}),
    ...(typeof value.title === "string" ? { title: value.title } : {}),
    s3_key: value.s3_key,
    ...(typeof value.mime_type === "string" ? { mime_type: value.mime_type } : {}),
    ...(typeof value.alt_text === "string" ? { alt_text: value.alt_text } : {})
  };
}

function isCaseGraphic(value: CaseGraphic | undefined): value is CaseGraphic {
  return value !== undefined;
}

function normalizeAgentMetadata(value: Record<string, unknown>): CaseAgentMetadata {
  return {
    ...(typeof value.runtime === "string" ? { runtime: value.runtime } : {}),
    ...(typeof value.framework === "string" ? { framework: value.framework } : {}),
    ...(typeof value.model_id === "string" ? { model_id: value.model_id } : {}),
    ...(Array.isArray(value.tool_calls)
      ? { tool_calls: value.tool_calls.filter((toolName): toolName is string => typeof toolName === "string") }
      : {})
  };
}

function logAgentResponse(mode: CaseAgentRequest["mode"], response: CaseAgentResponse): void {
  console.info("agentcore_response", {
    mode,
    ok: response.ok,
    action: response.action,
    caseId: response.case_id,
    syncedCount: response.synced_count,
    ingestionStatus: response.ingestion_status,
    runtime: response.agent?.runtime,
    framework: response.agent?.framework,
    modelId: response.agent?.model_id,
    toolCalls: response.agent?.tool_calls
  });
}

function parseJsonObject(value: string): Record<string, unknown> | undefined {
  const trimmed = value.trim();
  if (!trimmed) {
    return undefined;
  }

  for (const candidate of [trimmed, extractFirstJsonObject(trimmed)]) {
    if (!candidate) {
      continue;
    }
    try {
      const parsed: unknown = JSON.parse(candidate);
      if (isRecord(parsed)) {
        return parsed;
      }
    } catch {
      // Continue with the next candidate.
    }
  }

  return undefined;
}

function extractFirstJsonObject(value: string): string | undefined {
  const start = value.indexOf("{");
  const end = value.lastIndexOf("}");
  return start >= 0 && end > start ? value.slice(start, end + 1) : undefined;
}

function formatAgentReply(response: CaseAgentResponse): string {
  if (response.slack_text?.trim()) {
    return formatSlackTextForReadability(response.slack_text);
  }
  if (response.ok === false) {
    const message = response.error
      ? `AgentCore Runtime 側で処理できませんでした: ${response.error}`
      : "AgentCore Runtime 側で処理できませんでした。ログを確認します。";
    return formatSlackTextForReadability(message);
  }
  return formatHelpReply();
}

export function formatSlackTextForReadability(text: string): string {
  const compactLines = text
    // AgentCoreのツール引数で二重エスケープされた改行をSlack投稿前に復元する
    .replace(/\\r\\n|\\n|\\r/g, "\n")
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => line.trimEnd());
  const lines = trimBlankEdges(collapseBlankLines(compactLines));
  const output: string[] = [];
  let previousTextLine = "";

  for (const line of lines) {
    if (!line.trim()) {
      if (output.length > 0 && output.at(-1) !== "") {
        output.push("");
      }
      previousTextLine = "";
      continue;
    }

    if (shouldInsertSlackBlankLine(previousTextLine, line, output) && output.at(-1) !== "") {
      output.push("");
    }
    output.push(line);
    previousTextLine = line;
  }

  return trimBlankEdges(collapseBlankLines(output)).join("\n");
}

function shouldInsertSlackBlankLine(previous: string, current: string, output: readonly string[]): boolean {
  if (!previous || output.length < 1) {
    return false;
  }
  if (isSlackBullet(previous) && isSlackBullet(current)) {
    return false;
  }
  if (isSlackNumbered(previous) && isSlackIndentedBullet(current)) {
    return false;
  }
  if (isSlackIndentedBullet(previous) && isSlackIndentedBullet(current)) {
    return false;
  }
  if (isSlackSectionLead(current)) {
    return true;
  }
  if (isSlackBullet(current) && !isSlackBullet(previous) && !isSlackNumbered(previous) && !isSlackIndentedBullet(previous)) {
    return true;
  }
  if (!isSlackBullet(current) && !isSlackIndentedBullet(current) && (isSlackBullet(previous) || isSlackIndentedBullet(previous))) {
    return true;
  }
  if (isSlackNumbered(current) && !isSlackNumbered(previous)) {
    return true;
  }
  return false;
}

function isSlackSectionLead(line: string): boolean {
  return /^(ちなみに|詳しく|もう少し|追加情報|最終更新|使い方の例|このアプリは|メールアドレス|※)/.test(line.trim());
}

function isSlackBullet(line: string): boolean {
  return /^[・*-]\s*/.test(line.trim());
}

function isSlackIndentedBullet(line: string): boolean {
  return /^\s+[・*-]\s*/.test(line);
}

function isSlackNumbered(line: string): boolean {
  return /^\d+\.\s+/.test(line.trim());
}

function collapseBlankLines(lines: readonly string[]): string[] {
  const output: string[] = [];
  for (const line of lines) {
    if (!line.trim() && output.at(-1) === "") {
      continue;
    }
    output.push(line);
  }
  return output;
}

function trimBlankEdges(lines: readonly string[]): string[] {
  let start = 0;
  let end = lines.length;
  while (start < end && !lines[start]?.trim()) {
    start += 1;
  }
  while (end > start && !lines[end - 1]?.trim()) {
    end -= 1;
  }
  return lines.slice(start, end);
}

async function recordDailyShare(caseId: string, channelId: string, messageTs: string | undefined): Promise<void> {
  const sharedAt = new Date().toISOString();
  await dynamodb.send(
    new PutCommand({
      TableName: requiredEnv("SHARE_LOG_TABLE_NAME"),
      Item: {
        case_id: caseId,
        shared_at: sharedAt,
        channel_id: channelId,
        ...(messageTs ? { message_ts: messageTs } : {})
      }
    })
  );
  await dynamodb.send(
    new UpdateCommand({
      TableName: requiredEnv("CASES_TABLE_NAME"),
      Key: { case_id: caseId },
      UpdateExpression: "SET last_shared_at = :sharedAt",
      ExpressionAttributeValues: { ":sharedAt": sharedAt }
    })
  );
  if (messageTs) {
    await dynamodb.send(
      new PutCommand({
        TableName: requiredEnv("THREAD_STATE_TABLE_NAME"),
        Item: buildSharedCaseThreadStateItem({
          caseId,
          channelId,
          messageTs,
          sharedAt,
          expiresAt: epochSeconds() + THREAD_STATE_TTL_SECONDS
        })
      })
    );
  }
}

export function buildSharedCaseThreadStateItem(input: {
  readonly caseId: string;
  readonly channelId: string;
  readonly messageTs: string;
  readonly sharedAt: string;
  readonly expiresAt: number;
}): {
  readonly thread_key: string;
  readonly case_id: string;
  readonly channel_id: string;
  readonly thread_ts: string;
  readonly updated_at: string;
  readonly expires_at: number;
} {
  return {
    thread_key: buildThreadKey(input.channelId, input.messageTs),
    case_id: input.caseId,
    channel_id: input.channelId,
    thread_ts: input.messageTs,
    updated_at: input.sharedAt,
    expires_at: input.expiresAt
  };
}

function buildThreadKey(channelId: string, threadTs: string): string {
  return `${channelId}:${threadTs}`;
}

async function postThreadReply(channel: string, threadTs: string, text: string): Promise<ChatPostMessageResult> {
  return postSlackMessage({
    channel,
    thread_ts: threadTs,
    text: formatSlackTextForReadability(text)
  });
}

async function postAgentResponseToThread(channel: string, threadTs: string, response: CaseAgentResponse): Promise<void> {
  await postThreadReply(channel, threadTs, formatAgentReply(response));
  const graphics = graphicsForThreadResponse(response);
  if (graphics.length === 0) {
    return;
  }

  try {
    await uploadGraphicsToSlack(channel, threadTs, graphics);
  } catch (error) {
    console.error("slack_graphic_upload_failed", { error: toSafeError(error), action: response.action, caseId: response.case_id });
  }
}

async function tryPostThreadReply(channel: string, threadTs: string, text: string): Promise<void> {
  try {
    await postThreadReply(channel, threadTs, text);
  } catch (error) {
    console.error("slack_error_reply_failed", { error: toSafeError(error) });
  }
}

async function postChannelMessage(channel: string, text: string): Promise<ChatPostMessageResult> {
  return postSlackMessage({ channel, text: formatSlackTextForReadability(text) });
}

async function postDailyShare(channel: string, response: CaseAgentResponse): Promise<ChatPostMessageResult> {
  const slackText = formatSlackTextForReadability(response.slack_text ?? "");
  const graphic = response.graphic_s3_key
    ? singleGraphicFromResponse(response)
    : undefined;
  if (!graphic) {
    return postChannelMessage(channel, slackText);
  }

  try {
    return await uploadGraphicToSlack(channel, undefined, graphic, slackText);
  } catch (error) {
    console.error("slack_daily_graphic_upload_failed", { error: toSafeError(error), caseId: response.case_id });
    return postChannelMessage(channel, slackText);
  }
}

async function postSlackMessage(body: Record<string, unknown>): Promise<ChatPostMessageResult> {
  return callSlackApi<ChatPostMessageResult>("chat.postMessage", {
    unfurl_links: false,
    unfurl_media: false,
    ...body
  });
}

async function callSlackApi<T extends object>(method: string, body: Record<string, unknown>): Promise<T> {
  const token = await getBotToken();
  const requestBody = slackApiRequestBody(method, body);
  const response = await fetch(`https://slack.com/api/${method}`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": requestBody.contentType
    },
    body: requestBody.body
  });
  const data = (await response.json()) as { readonly ok?: boolean; readonly error?: string } & T;
  if (!response.ok || data.ok !== true) {
    throw new Error(`Slack API ${method} failed: ${data.error ?? response.statusText}`);
  }
  return data;
}

// JSON POST を受け付けないメソッド（読み取り系・アップロードURL取得）は
// application/x-www-form-urlencoded で送る。conversations.replies は
// JSON で送ると invalid_arguments になる（2026-07-08 実機で確認）
const URLENCODED_SLACK_METHODS = new Set([
  "files.getUploadURLExternal",
  "conversations.replies",
  "conversations.history",
  "conversations.info"
]);

function slackApiRequestBody(method: string, body: Record<string, unknown>): { readonly contentType: string; readonly body: string } {
  if (URLENCODED_SLACK_METHODS.has(method)) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(body)) {
      if (value !== undefined) {
        params.set(key, String(value));
      }
    }
    return {
      contentType: "application/x-www-form-urlencoded",
      body: params.toString()
    };
  }

  return {
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body)
  };
}

export function graphicsForThreadResponse(response: CaseAgentResponse): CaseGraphic[] {
  if (response.action === "search") {
    return [...(response.graphics ?? [])].slice(0, 3);
  }
  if ((response.action === "describe" || response.action === "graphic") && response.graphic_s3_key) {
    return [singleGraphicFromResponse(response)];
  }
  return [];
}

function singleGraphicFromResponse(response: CaseAgentResponse): CaseGraphic {
  return {
    ...(response.case_id ? { case_id: response.case_id } : {}),
    ...(response.graphic_alt_text ? { title: response.graphic_alt_text } : {}),
    s3_key: response.graphic_s3_key ?? "",
    ...(response.graphic_mime_type ? { mime_type: response.graphic_mime_type } : {}),
    ...(response.graphic_alt_text ? { alt_text: response.graphic_alt_text } : {})
  };
}

async function uploadGraphicsToSlack(channel: string, threadTs: string, graphics: readonly CaseGraphic[]): Promise<void> {
  for (const graphic of graphics) {
    await uploadGraphicToSlack(channel, threadTs, graphic);
  }
}

async function uploadGraphicToSlack(
  channel: string,
  threadTs: string | undefined,
  graphic: CaseGraphic,
  initialComment?: string
): Promise<ChatPostMessageResult> {
  const { body, contentType } = await readGraphicObject(graphic);
  const filename = buildGraphicFilename(graphic, contentType);
  const uploadUrl = await callSlackApi<SlackUploadUrlResult>("files.getUploadURLExternal", {
    filename,
    length: body.length
  });
  if (!uploadUrl.upload_url || !uploadUrl.file_id) {
    throw new Error("Slack upload URL response was missing upload_url or file_id");
  }

  const uploadResponse = await fetch(uploadUrl.upload_url, {
    method: "POST",
    headers: {
      "content-type": contentType,
      "content-length": String(body.length)
    },
    body
  });
  if (!uploadResponse.ok) {
    throw new Error(`Slack file binary upload failed: ${uploadResponse.status}`);
  }

  const completeUpload = await callSlackApi<SlackCompleteUploadResult>("files.completeUploadExternal", {
    files: [
      {
        id: uploadUrl.file_id,
        title: graphic.title ?? graphic.alt_text ?? "社内事例グラレコ"
      }
    ],
    channel_id: channel,
    ...(threadTs ? { thread_ts: threadTs } : {}),
    ...(initialComment ? { initial_comment: formatSlackTextForReadability(initialComment) } : {})
  });

  const uploadedMessageTs = extractFileShareTs(completeUpload, channel);
  return { ...(uploadedMessageTs ? { ts: uploadedMessageTs } : {}) };
}

export function extractFileShareTs(result: SlackCompleteUploadResult, channel: string): string | undefined {
  const files = [...(result.files ?? []), ...(result.file ? [result.file] : [])];
  for (const file of files) {
    const shares = file.shares;
    if (!shares) {
      continue;
    }
    for (const visibility of ["public", "private"] as const) {
      const channelShares = shares[visibility]?.[channel] ?? [];
      const share = channelShares.find((candidate) => typeof candidate.ts === "string" && candidate.ts.length > 0);
      if (share?.ts) {
        return share.ts;
      }
    }
  }
  return undefined;
}

async function readGraphicObject(graphic: CaseGraphic): Promise<{ readonly body: Buffer; readonly contentType: string }> {
  const response = await s3.send(
    new GetObjectCommand({
      Bucket: requiredEnv("CASE_ASSETS_BUCKET_NAME"),
      Key: graphic.s3_key
    })
  );
  const bytes = response.Body ? await response.Body.transformToByteArray() : new Uint8Array();
  if (bytes.length === 0) {
    throw new Error("Graphic object was empty");
  }
  return {
    body: Buffer.from(bytes),
    contentType: response.ContentType ?? graphic.mime_type ?? "image/png"
  };
}

function buildGraphicFilename(_graphic: CaseGraphic, contentType: string): string {
  const extension = contentType === "image/jpeg" ? "jpg" : "png";
  return `jirei-share-graphic.${extension}`;
}

async function getSigningSecret(): Promise<string> {
  if (cachedSigningSecret) {
    return cachedSigningSecret;
  }

  const name = requiredEnv("SLACK_SIGNING_SECRET_PARAMETER_NAME");
  const response = await ssm.send(new GetParameterCommand({ Name: name, WithDecryption: true }));
  const value = response.Parameter?.Value;
  if (!value) {
    throw new Error("Slack signing secret parameter is empty");
  }
  cachedSigningSecret = value;
  return value;
}

async function getBotToken(): Promise<string> {
  if (cachedBotToken) {
    return cachedBotToken;
  }

  const name = requiredEnv("SLACK_BOT_TOKEN_PARAMETER_NAME");
  const response = await ssm.send(new GetParameterCommand({ Name: name, WithDecryption: true }));
  const value = response.Parameter?.Value;
  if (!value) {
    throw new Error("Slack bot token parameter is empty");
  }
  cachedBotToken = value;
  return value;
}

function formatHelpReply(prefix?: string): string {
  return formatSlackTextForReadability(
    [
      ...(prefix ? [prefix, ""] : []),
      "このアプリは現在、社内の案件情報・AI活用事例の概要登録と検索に対応しています。",
      "登録済みの事例への追記や、直前に検索した事例の詳細確認もできます。",
      "",
      "使い方の例",
      "・登録: 「この取り組みを事例として登録して。〇〇案件で、□□チームが△△をしました」",
      "・検索: 「議事録AIっぽい事例ある？」「エネルギー業界向けのAI活用を教えて」",
      "・追記: 登録済み事例のスレッドで、Bot にメンションして追加情報を書いてください。",
      "",
      "メールアドレス、電話番号、顧客担当者名、APIキーなどは投稿・保存しないように扱います。"
    ].join("\n")
  );
}

function truncateSlackText(text: string, maxLength = 700): string {
  const normalized = text.replace(/\s+/g, " ").trim();
  return normalized.length <= maxLength ? normalized : `${normalized.slice(0, maxLength - 1)}…`;
}

function epochSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

function isDailyShareEvent(
  event: HandlerEvent
): event is DailyShareEvent {
  return "mode" in event && event.mode === "daily_share";
}

function isGraphicMaintenanceEvent(
  event: HandlerEvent
): event is GraphicMaintenanceEvent {
  return "mode" in event && event.mode === "graphic_maintenance";
}

function isKnowledgeBaseSyncEvent(
  event: HandlerEvent
): event is KnowledgeBaseSyncEvent {
  return "mode" in event && event.mode === "knowledge_base_sync";
}

function isProcessSlackEventInvocation(
  event: HandlerEvent
): event is ProcessSlackEventInvocation {
  return "mode" in event && event.mode === "process_slack_event";
}

function isSqsEvent(event: HandlerEvent): event is SQSEvent {
  return (
    "Records" in event &&
    Array.isArray(event.Records) &&
    event.Records.every((record) => record.eventSource === "aws:sqs")
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toSafeError(error: unknown): { readonly name: string; readonly message: string } {
  if (error instanceof Error) {
    const message = error.message.length <= 200 ? error.message : `${error.message.slice(0, 199)}…`;
    return { name: error.name, message };
  }
  return { name: "UnknownError", message: "unknown" };
}

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

function json(statusCode: number, body: unknown): APIGatewayProxyStructuredResultV2 {
  return {
    statusCode,
    headers: { "content-type": "application/json; charset=utf-8" },
    body: JSON.stringify(body)
  };
}
