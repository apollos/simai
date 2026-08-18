/** Simai native plugin for OpenClaw 2026.7.1. */

import {
  CorrelationStore,
  matchBinding,
  normalizeSenderKey,
  ownerFallbackBinding,
} from "./identity.js";
import { randomBytes } from "node:crypto";
import { homedir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import { submitWithEncryptedFallback, type InboxSubmission } from "./inbox.js";
import { SimaiCoreClient } from "./core.js";
import { readOwnerOnlyToken } from "./credentials.js";
import type {
  AgentToolResult,
  CaptureMode,
  OpenClawPluginApi,
  OpenClawPluginDefinition,
  OpenClawPluginToolContext,
  SimaiBinding,
  SimaiPluginConfig,
  VerifiedIdentity,
} from "./types.js";

// Dictation ("driving") mode: sticky verbatim capture with no model filtering.
// Triggers are anchored full-sentence matches so ordinary chat never toggles
// the mode by accident.
const DRIVING_ON =
  /^(?:我(?:现在)?在开车|(?:进入|开启|打开)(?:驾驶|速记|记录)模式|(?:开始|开启)(?:速记|记录)|我有(?:个|一些)?想法[，,、；;：:\s]*(?:请|帮我)?(?:只)?记录)(?:[，,、；;：:\s]*(?:接下来)?(?:请)?(?:只记录|只帮我记录|不要回复|不需要回复))?[。！!]?$/;
const DRIVING_OFF =
  /^(?:结束|退出|停止|关闭)(?:驾驶|速记|记录)模式[。！!]?$|^(?:我)?(?:已经)?(?:停车了|不开车了)[。！!]?$|^记录(?:完毕|结束)[。！!]?$|^(?:结束|停止|关闭)记录[。！!]?$/;
const DEFAULT_CORRELATION_WINDOW_MS = 5 * 60 * 1000;

const plugin: OpenClawPluginDefinition = {
  id: "simai",
  name: "思脉",
  description: "将精确绑定来源的想法送入加密待确认箱，并提供思维树工具。",
  version: "0.1.0",
  register(api) {
    registerSimai(api);
  },
};

export default plugin;

/**
 * The gateway registers the plugin several times inside one process (per
 * agent/run plus tool discovery), and hooks and tool calls can land on
 * different registrations. Correlation and session state must therefore be
 * process-global, not per-registration.
 */
let sharedCorrelations: { windowMs: number; store: CorrelationStore } | undefined;
const sharedVerifiedSessions = new Map<string, VerifiedIdentity>();
// bindingId -> dictation session id: every message captured while the mode is
// on carries this id so the core can merge the session into ONE topic.
const sharedDrivingMode = new Map<string, string>();
const sharedRecentlyCaptured = new Map<string, number>();

const newDictationId = (): string =>
  `${Date.now().toString(36)}-${randomBytes(6).toString("hex")}`;

/** Test isolation only. */
export function resetSimaiSharedState(): void {
  sharedCorrelations = undefined;
  sharedVerifiedSessions.clear();
  sharedDrivingMode.clear();
  sharedRecentlyCaptured.clear();
}

function registerSimai(api: OpenClawPluginApi): void {
  const config = parsePluginConfig(api.pluginConfig);
  // Distinguishes plugin instances in logs: the host may register the plugin
  // several times (gateway + per-run), and in-memory correlation only works
  // when both hooks and the tool call land on the same instance.
  const instanceId = Math.random().toString(36).slice(2, 8);
  const probeMode = config.probeMode === true;
  const enabledBindings = probeMode
    ? []
    : config.bindings.filter((binding) => binding.enabled);
  // Metadata-only deployment probe. Values identify the source (needed to fill
  // in bindings); message bodies are reduced to a length and a placeholder flag.
  const probeLog = (hook: string, fields: Record<string, unknown>): void => {
    api.logger.info(`simai[probe] ${hook} ${JSON.stringify(fields)}`);
  };
  const correlationWindowMs = config.correlationWindowMs ?? DEFAULT_CORRELATION_WINDOW_MS;
  if (!sharedCorrelations || sharedCorrelations.windowMs !== correlationWindowMs) {
    sharedCorrelations = {
      windowMs: correlationWindowMs,
      store: new CorrelationStore(correlationWindowMs),
    };
  }
  const correlations = sharedCorrelations.store;
  const verifiedSessions = sharedVerifiedSessions;
  const drivingMode = sharedDrivingMode;
  let core: SimaiCoreClient | undefined;

  // Never rely on a host-provided path helper: real gateways may lack
  // api.resolvePath or return nothing for absolute inputs.
  const expandPath = (input: string): string => {
    if (input === "~") return homedir();
    if (input.startsWith("~/")) return join(homedir(), input.slice(2));
    return isAbsolute(input) ? input : resolve(input);
  };

  const submitEncrypted = (item: InboxSubmission): Promise<boolean> =>
    submitWithEncryptedFallback(
      expandPath(config.inboxSocket),
      {
        vaultHeaderPath: expandPath(config.vaultHeaderPath),
        inboxDir: expandPath(config.inboxDir),
      },
      item,
    );

  const getCore = (): SimaiCoreClient => {
    if (core) return core;
    const tokenPath = expandPath(config.coreTokenFile);
    const coreToken = readOwnerOnlyToken(tokenPath);
    core = new SimaiCoreClient(config.coreUrl.replace(/\/$/, ""), coreToken);
    return core;
  };

  const rememberVerified = (identity: VerifiedIdentity): void => {
    const cutoff = Date.now() - correlationWindowMs;
    for (const [key, value] of verifiedSessions) {
      if (value.receivedAt < cutoff) verifiedSessions.delete(key);
    }
    if (identity.sessionKey) verifiedSessions.set(identity.sessionKey, identity);
  };

  const authorizeTool = (ctx: OpenClawPluginToolContext): VerifiedIdentity | null => {
    // Deny logs carry identity metadata only (never message content) so a
    // rejected tool call can be diagnosed from the gateway log.
    const deny = (reason: string): null => {
      api.logger.warn(`simai: tool auth denied reason=${reason} instance=${instanceId}`);
      return null;
    };
    api.logger.info(
      `simai: tool auth ctx instance=${instanceId} channel=${ctx.messageChannel ?? "<none>"} ` +
        `account=${ctx.agentAccountId ?? "<none>"} requester=${ctx.requesterSenderId ?? "<none>"} ` +
        `to=${ctx.deliveryContext?.to ?? "<none>"} hasSession=${Boolean(ctx.sessionKey)} ` +
        `owner=${String(ctx.senderIsOwner)}`,
    );
    if (!ctx.sessionKey) return deny("missing_session_key");
    const identity = verifiedSessions.get(ctx.sessionKey);
    if (!identity) return deny("session_not_correlated");
    if (Date.now() - identity.receivedAt > correlationWindowMs) return deny("correlation_expired");
    const binding = enabledBindings.find((item) => item.id === identity.bindingId);
    if (!binding) return deny("binding_disabled");
    // Owner-only channels (allowMissingIdentity) legitimately omit ambient
    // identity in the tool context; present fields must still match exactly.
    const lax = binding.allowMissingIdentity === true;
    // The host's owner flag is channel-dependent (weixin reports false for the
    // bound sender). Strict bindings are authorized by the exact correlated
    // identity tuple below; only identity-less owner channels require it.
    if (lax && ctx.senderIsOwner === false) return deny("sender_not_owner");

    const channel = exactAmbient(ctx.messageChannel, ctx.deliveryContext?.channel);
    const accountId = exactAmbient(ctx.agentAccountId, ctx.deliveryContext?.accountId);
    if (conflicts(ctx.messageChannel, ctx.deliveryContext?.channel)) {
      return deny("ambiguous_channel");
    }
    if (conflicts(ctx.agentAccountId, ctx.deliveryContext?.accountId)) {
      return deny("ambiguous_account");
    }
    if (channel) {
      if (channel !== identity.channel) return deny(`channel_mismatch got=${channel}`);
    } else if (!lax) {
      return deny("missing_channel");
    }
    if (accountId) {
      if (accountId !== identity.accountId) return deny(`account_mismatch got=${accountId}`);
    } else if (!lax) {
      return deny("missing_account");
    }
    if (ctx.requesterSenderId) {
      if (ctx.requesterSenderId !== identity.senderKey) {
        return deny(`sender_mismatch got=${ctx.requesterSenderId}`);
      }
    } else if (
      !lax &&
      !(identity.conversationId !== null && ctx.deliveryContext?.to === identity.conversationId)
    ) {
      // The host does not always forward a requester id. Accept the omission
      // only when the delivery target is exactly the bound private
      // conversation, which the correlated session already pinned.
      return deny("missing_requester_sender");
    }
    if (identity.conversationId !== null && ctx.deliveryContext?.to !== identity.conversationId) {
      if (!(lax && ctx.deliveryContext?.to === undefined)) {
        return deny(`conversation_mismatch got=${ctx.deliveryContext?.to ?? "<none>"}`);
      }
    }
    return identity;
  };

  const inboxItem = (
    identity: VerifiedIdentity,
    body: string,
    captureMode: CaptureMode,
    dictationId: string | null = null,
  ): InboxSubmission => ({
    binding_id: identity.bindingId,
    channel: identity.channel,
    account_id: identity.accountId,
    sender_key: identity.senderKey,
    conversation_id: identity.conversationId,
    is_group: identity.isGroup,
    capture_mode: captureMode,
    message_id: identity.messageId,
    session_key: identity.sessionKey,
    dictation_id: dictationId,
    body,
  });

  // The host can deliver both hooks more than once for the same message;
  // capture each correlated message at most once per window.
  const recentlyCaptured = sharedRecentlyCaptured;
  const alreadyCaptured = (matched: VerifiedIdentity): boolean => {
    const now = Date.now();
    for (const [key, at] of recentlyCaptured) {
      if (now - at > correlationWindowMs) recentlyCaptured.delete(key);
    }
    const key = `${matched.bindingId}\u0000${matched.messageId}`;
    if (recentlyCaptured.has(key)) return true;
    recentlyCaptured.set(key, now);
    return false;
  };
  const alreadySealedAssistant = (bindingId: string, messageId: string): boolean => {
    const key = `assistant\u0000${bindingId}\u0000${messageId}`;
    if (recentlyCaptured.has(key)) return true;
    recentlyCaptured.set(key, Date.now());
    return false;
  };

  const handleMatched = async (
    matched: VerifiedIdentity & { body: string },
  ): Promise<void> => {
    rememberVerified(matched);
    const body = matched.body.trim();
    if (!body) return;
    // Host commands ("/reset", "/help", ...) are operator instructions, never
    // thoughts; keep them out of the sealed inbox in every capture mode.
    if (body.startsWith("/")) return;
    if (alreadyCaptured(matched)) {
      api.logger.info(
        `simai: duplicate correlation ignored messageId=${matched.messageId}`,
      );
      return;
    }

    if (DRIVING_ON.test(body)) {
      const dictationId = newDictationId();
      drivingMode.set(matched.bindingId, dictationId);
      api.logger.info(
        `simai: driving mode enabled binding=${matched.bindingId} session=${dictationId}`,
      );
      return;
    }
    if (DRIVING_OFF.test(body)) {
      const closingId = drivingMode.get(matched.bindingId);
      drivingMode.delete(matched.bindingId);
      api.logger.info(`simai: driving mode disabled binding=${matched.bindingId}`);
      if (closingId) {
        // The owner's 结束记录 is the authoritative end-of-session signal:
        // notify the core so the merge runs now instead of after the daily
        // cutoff quiet window. Any failure here is harmless - the core's
        // quiet-window fallback still processes the session later.
        try {
          const res = await getCore().closeDictation(matched.bindingId, closingId);
          api.logger.info(
            `simai: dictation close notified binding=${matched.bindingId} ` +
              `session=${closingId} ok=${res.ok}` +
              (res.ok ? ` processing=${res.data?.processing === true}` : ""),
          );
        } catch {
          api.logger.warn(
            `simai: dictation close notify failed binding=${matched.bindingId}; ` +
              "core will merge after the quiet window",
          );
        }
      }
      return;
    }

    const dictationId = drivingMode.get(matched.bindingId);
    if (dictationId) {
      const ok = await submitEncrypted(
        inboxItem(matched, matched.body, "explicit", dictationId),
      );
      if (!ok) {
        api.logger.warn(
          `simai: driving-mode sealed inbox submission failed binding=${matched.bindingId}`,
        );
      } else {
        api.logger.info(
          `simai: explicit capture sealed binding=${matched.bindingId} messageId=${matched.messageId}`,
        );
      }
      return;
    }

    const binding = enabledBindings.find((item) => item.id === matched.bindingId);
    if (!binding?.passiveCapture) return;
    const ok = await submitEncrypted(inboxItem(matched, matched.body, "passive"));
    if (!ok) {
      api.logger.warn(`simai: sealed inbox submission failed binding=${matched.bindingId}`);
    } else {
      api.logger.info(
        `simai: passive capture sealed binding=${matched.bindingId} messageId=${matched.messageId}`,
      );
    }
  };

  if (api.registrationMode === "full") {
    // Phase one: typed lifecycle hook carries the authoritative OpenClaw
    // channel/account/sender tuple. The group flag is verified in phase two.
    api.on("message_received", async (event, ctx) => {
      if (probeMode) {
        probeLog("message_received", {
          channelId: ctx.channelId ?? null,
          accountId: ctx.accountId ?? null,
          senderId: ctx.senderId ?? event.senderId ?? null,
          from: event.from ?? null,
          conversationId: ctx.conversationId ?? null,
          messageId: event.messageId ?? ctx.messageId ?? null,
          hasSessionKey: Boolean(event.sessionKey ?? ctx.sessionKey),
          contentLength: event.content?.length ?? 0,
        });
        return;
      }
      if (conflicts(event.messageId, ctx.messageId)) return;
      if (conflicts(event.sessionKey, ctx.sessionKey)) return;
      const messageId = event.messageId ?? ctx.messageId;
      const sessionKey = event.sessionKey ?? ctx.sessionKey;
      const senderKey = normalizeSenderKey({
        senderId: ctx.senderId ?? event.senderId,
        from: event.from,
      });
      const source = {
        channel: ctx.channelId,
        accountId: ctx.accountId,
        senderId: senderKey,
        conversationId: ctx.conversationId ?? null,
      };
      const binding = matchBinding(enabledBindings, source, { allowUnknownGroup: true });
      if (!binding || !messageId) return;
      const lax = binding.allowMissingIdentity === true;
      const effectiveSender = senderKey ?? (lax ? binding.senderKey : undefined);
      const effectiveAccount = ctx.accountId ?? (lax ? binding.accountId : undefined);
      const effectiveConversation =
        ctx.conversationId ?? (lax ? binding.conversationId ?? null : null);
      if (!effectiveSender || !effectiveAccount) return;

      const matched = correlations.addEnvelope({
        binding,
        channel: ctx.channelId,
        accountId: effectiveAccount,
        senderKey: effectiveSender,
        conversationId: effectiveConversation,
        messageId,
        sessionKey,
        receivedAt: Date.now(),
      });
      api.logger.info(
        `simai: hook envelope instance=${instanceId} binding=${binding.id} ` +
          `messageId=${messageId} matched=${Boolean(matched)}`,
      );
      if (matched) await handleMatched(matched);
    });

    // Phase two: the internal hook fires after the configured ASR/media
    // preprocessing pipeline and supplies the exact body sent to the agent.
    api.registerHook(
      "message:preprocessed",
      async (event) => {
        if (event.type !== "message" || event.action !== "preprocessed") return;
        if (probeMode) {
          const probeBody = event.context.bodyForAgent ?? "";
          probeLog("message_preprocessed", {
            channelId: event.context.channelId ?? null,
            senderId: event.context.senderId ?? null,
            from: event.context.from ?? null,
            conversationId: event.context.conversationId ?? null,
            isGroup: event.context.isGroup ?? null,
            messageId: event.context.messageId ?? null,
            hasSessionKey: Boolean(event.sessionKey),
            bodyLength: probeBody.length,
            bodyLooksLikeMediaPlaceholder: /^\s*\[(audio|voice|video|image)/i.test(probeBody),
          });
          return;
        }
        const body = event.context.bodyForAgent;
        let senderKey = normalizeSenderKey({
          senderId: event.context.senderId,
          from: event.context.from,
        });
        let conversationId = event.context.conversationId ?? null;
        if (!senderKey) {
          const fallback = ownerFallbackBinding(enabledBindings, event.context.channelId);
          if (fallback) {
            senderKey = fallback.senderKey;
            conversationId = conversationId ?? fallback.conversationId ?? null;
          }
        }
        if (!body?.trim() || !senderKey) return;
        const matched = correlations.addBody({
          body,
          channel: event.context.channelId,
          senderKey,
          conversationId,
          isGroup: event.context.isGroup,
          messageId: event.context.messageId,
          sessionKey: event.sessionKey,
          receivedAt: Date.now(),
        });
        api.logger.info(
          `simai: hook body instance=${instanceId} ` +
            `messageId=${event.context.messageId ?? "<none>"} matched=${Boolean(matched)}`,
        );
        if (matched) await handleMatched(matched);
      },
      {
        name: "simai-message-preprocessed",
        description: "Capture only the final ASR/media-aware user body after exact identity correlation.",
      },
    );

    // Dictation context: while a binding is in dictation mode, the assistant's
    // delivered replies are sealed too (speaker=assistant, same dictation_id).
    // The daily merge only keeps assistant points the owner explicitly
    // endorsed, but without this context an owner's "对，就按这个" would lose
    // its referent entirely.
    try {
      api.on("message_sent", async (event, ctx) => {
        if (probeMode) {
          probeLog("message_sent", {
            channelId: ctx.channelId ?? null,
            accountId: ctx.accountId ?? null,
            to: event.to ?? null,
            conversationId: ctx.conversationId ?? null,
            messageId: event.messageId ?? ctx.messageId ?? null,
            hasSessionKey: Boolean(event.sessionKey ?? ctx.sessionKey),
            success: event.success,
            contentLength: event.content?.length ?? 0,
          });
          return;
        }
        if (drivingMode.size === 0) return;
        if (event.success === false) return;
        const body = event.content?.trim();
        if (!body) return;
        const target = event.to ?? ctx.conversationId;
        const matches = enabledBindings.filter((binding) => {
          if (!drivingMode.has(binding.id)) return false;
          if (ctx.channelId && binding.channel !== ctx.channelId) return false;
          if (ctx.accountId && binding.accountId !== ctx.accountId) return false;
          if (
            binding.conversationId !== undefined &&
            target !== undefined &&
            target !== binding.conversationId
          ) {
            return false;
          }
          return true;
        });
        // Ambiguity fails closed: an outbound reply must map to exactly one
        // dictating binding or it is not captured at all.
        if (matches.length !== 1) return;
        const binding = matches[0];
        const dictationId = drivingMode.get(binding.id);
        if (!dictationId) return;
        const messageId = event.messageId ?? ctx.messageId ?? null;
        const sessionKey = event.sessionKey ?? ctx.sessionKey ?? null;
        if (!messageId && !sessionKey) return;
        if (messageId && alreadySealedAssistant(binding.id, messageId)) return;
        const ok = await submitEncrypted({
          binding_id: binding.id,
          channel: binding.channel,
          account_id: binding.accountId,
          sender_key: binding.senderKey,
          conversation_id: binding.conversationId ?? null,
          is_group: false,
          capture_mode: "explicit",
          message_id: messageId,
          session_key: sessionKey,
          dictation_id: dictationId,
          speaker: "assistant",
          body,
        });
        if (!ok) {
          api.logger.warn(
            `simai: assistant reply sealing failed binding=${binding.id}`,
          );
        } else {
          api.logger.info(
            `simai: assistant reply sealed binding=${binding.id} ` +
              `messageId=${messageId ?? "<none>"} session=${dictationId}`,
          );
        }
      });
    } catch {
      api.logger.warn(
        "simai: host does not support the message_sent hook; assistant replies " +
          "will not be captured during dictation",
      );
    }
  }

  if (api.registrationMode === "full" || api.registrationMode === "tool-discovery") {
    registerTools(api, getCore, authorizeTool, drivingMode, submitEncrypted, inboxItem);
  }

  api.logger.info(
    `simai plugin registered mode=${api.registrationMode} bindings=${enabledBindings.length}` +
      ` instance=${instanceId}` +
      (probeMode ? " probeMode=on (capture disabled, metadata-only logging)" : ""),
  );
}

function registerTools(
  api: OpenClawPluginApi,
  getCore: () => SimaiCoreClient,
  authorize: (ctx: OpenClawPluginToolContext) => VerifiedIdentity | null,
  drivingMode: Map<string, string>,
  submitEncrypted: (item: InboxSubmission) => Promise<boolean>,
  inboxItem: (
    identity: VerifiedIdentity,
    body: string,
    captureMode: CaptureMode,
    dictationId?: string | null,
  ) => InboxSubmission,
): void {
  const deny = { error: "simai: source not whitelisted or message identity not correlated" };
  const locked = { status: "思脉处于锁定状态，内容已加密放入待确认箱。" };

  const register = (
    name: string,
    label: string,
    description: string,
    parameters: Record<string, unknown>,
    handler: (args: Record<string, unknown>, identity: VerifiedIdentity) => Promise<unknown>,
  ): void => {
    api.registerTool(
      (ctx) => ({
        name,
        label,
        description,
        parameters,
        async execute(_toolCallId, rawParams, signal) {
          if (signal?.aborted) return toolResult({ error: "simai: request aborted" });
          const identity = authorize(ctx);
          if (!identity) return toolResult(deny);
          const args = asRecord(rawParams);
          try {
            return toolResult(await handler(args, identity));
          } catch (error) {
            const message = error instanceof Error ? error.message : "unexpected error";
            return toolResult({ error: `simai: ${message}` });
          }
        },
      }),
      { names: [name] },
    );
  };

  register(
    "simai_capture",
    "记录思想",
    "主动记录一条思想；整理后返回确认卡，确认后才写入思维树。",
    objectSchema(
      { text: { type: "string", minLength: 1, description: "用户明确要求记录的内容" } },
      ["text"],
    ),
    async (args, identity) => {
      const text = requiredString(args, "text");
      const dictationId = drivingMode.get(identity.bindingId);
      if (dictationId) {
        const ok = await submitEncrypted(inboxItem(identity, text, "explicit", dictationId));
        return ok ? { status: "已进入加密待确认箱" } : { error: "暂存失败，请稍后再试" };
      }

      let result;
      try {
        result = await getCore().capture(identity.bindingId, text, identity.messageId);
      } catch {
        result = { ok: false, error: "core unavailable" };
      }
      if (!result.ok) {
        const ok = await submitEncrypted(inboxItem(identity, text, "explicit"));
        if (!ok) return { error: "思脉暂不可用且加密暂存失败" };
        return result.locked
          ? locked
          : { status: "思脉暂不可用，内容已加密放入待确认箱。" };
      }
      return { cards: result.data?.cards };
    },
  );

  register(
    "simai_list_candidates",
    "待确认思想",
    "查看当前来源的待确认候选思想。",
    objectSchema({}),
    async (_args, identity) => {
      const result = await getCore().listCandidates(identity.bindingId);
      if (result.locked) return locked;
      return result.ok ? result.data : { error: result.error };
    },
  );

  register(
    "simai_confirm_candidate",
    "处理候选思想",
    "确认、拒绝或暂缓一个候选思想。",
    objectSchema(
      {
        candidate_id: { type: "string", minLength: 1 },
        decision: { type: "string", enum: ["confirm", "reject", "snooze"] },
        parent_id: { type: "string" },
        edited_content: { type: "string" },
      },
      ["candidate_id", "decision"],
    ),
    async (args, identity) => {
      const candidateId = requiredString(args, "candidate_id");
      const decision = requiredString(args, "decision");
      if (!(["confirm", "reject", "snooze"] as string[]).includes(decision)) {
        return { error: "decision must be confirm, reject, or snooze" };
      }
      const result = await getCore().decideCandidate(
        identity.bindingId,
        candidateId,
        decision as "confirm" | "reject" | "snooze",
        optionalString(args, "parent_id"),
        optionalString(args, "edited_content"),
      );
      if (result.locked) return locked;
      return result.ok ? result.data : { error: result.error };
    },
  );

  register(
    "simai_search",
    "搜索思维树",
    "以关键词和语义搜索已确认的思维树。",
    objectSchema({ q: { type: "string", minLength: 1, maxLength: 2000 } }, ["q"]),
    async (args, identity) => {
      const result = await getCore().search(identity.bindingId, requiredString(args, "q"));
      if (result.locked) return locked;
      return result.ok ? result.data : { error: result.error };
    },
  );

  register(
    "simai_get_node",
    "读取思维节点",
    "读取一个思维节点、路径及其语义关系。",
    objectSchema({ node_id: { type: "string", minLength: 1 } }, ["node_id"]),
    async (args, identity) => {
      const result = await getCore().getNode(identity.bindingId, requiredString(args, "node_id"));
      if (result.locked) return locked;
      return result.ok ? result.data : { error: result.error };
    },
  );

  register(
    "simai_query",
    "询问思维树",
    "基于已确认的思想树回答问题，并引用节点与版本。",
    objectSchema({ question: { type: "string", minLength: 1 } }, ["question"]),
    async (args, identity) => {
      const result = await getCore().query(
        identity.bindingId,
        requiredString(args, "question"),
      );
      if (result.locked) return locked;
      return result.ok ? result.data : { error: result.error };
    },
  );

  register(
    "simai_status",
    "思脉状态",
    "查看思脉锁定状态、任务情况和候选数量。",
    objectSchema({}),
    async (_args, identity) => {
      const result = await getCore().status(identity.bindingId);
      return result.ok ? result.data : { error: result.error };
    },
  );
}

function parsePluginConfig(raw: Record<string, unknown> | undefined): SimaiPluginConfig {
  if (!raw) throw new Error("simai: plugins.entries.simai.config is required");
  const coreUrl = requiredString(raw, "coreUrl");
  const parsedUrl = new URL(coreUrl);
  if (!(["http:", "https:"] as string[]).includes(parsedUrl.protocol)) {
    throw new Error("simai: coreUrl must use http or https");
  }
  if (!(["127.0.0.1", "::1", "localhost"] as string[]).includes(parsedUrl.hostname)) {
    throw new Error("simai: coreUrl must be loopback-only");
  }
  if (!Array.isArray(raw.bindings) || raw.bindings.length === 0) {
    throw new Error("simai: bindings must be a non-empty array");
  }
  const bindings = raw.bindings.map((value, index) => parseBinding(value, index));
  if (new Set(bindings.map((binding) => binding.id)).size !== bindings.length) {
    throw new Error("simai: binding ids must be unique");
  }
  const correlationWindowMs = raw.correlationWindowMs;
  if (
    correlationWindowMs !== undefined &&
    (!Number.isSafeInteger(correlationWindowMs) ||
      (correlationWindowMs as number) < 1_000 ||
      (correlationWindowMs as number) > 30 * 60 * 1000)
  ) {
    throw new Error("simai: correlationWindowMs must be 1000..1800000");
  }
  if (raw.probeMode !== undefined && typeof raw.probeMode !== "boolean") {
    throw new Error("simai: probeMode must be boolean");
  }
  return {
    coreUrl,
    coreTokenFile: requiredString(raw, "coreTokenFile"),
    inboxSocket: requiredString(raw, "inboxSocket"),
    vaultHeaderPath: requiredString(raw, "vaultHeaderPath"),
    inboxDir: requiredString(raw, "inboxDir"),
    bindings,
    ...(correlationWindowMs === undefined
      ? {}
      : { correlationWindowMs: correlationWindowMs as number }),
    ...(raw.probeMode === undefined ? {} : { probeMode: raw.probeMode as boolean }),
  };
}

function parseBinding(raw: unknown, index: number): SimaiBinding {
  const value = asRecord(raw);
  const bool = (key: string): boolean => {
    if (typeof value[key] !== "boolean") {
      throw new Error(`simai: bindings[${index}].${key} must be boolean`);
    }
    return value[key] as boolean;
  };
  if (
    value.allowMissingIdentity !== undefined &&
    typeof value.allowMissingIdentity !== "boolean"
  ) {
    throw new Error(`simai: bindings[${index}].allowMissingIdentity must be boolean`);
  }
  return {
    id: requiredString(value, "id"),
    channel: requiredString(value, "channel"),
    accountId: requiredString(value, "accountId"),
    senderKey: requiredString(value, "senderKey"),
    ...(optionalString(value, "conversationId")
      ? { conversationId: optionalString(value, "conversationId") }
      : {}),
    allowGroup: bool("allowGroup"),
    passiveCapture: bool("passiveCapture"),
    enabled: bool("enabled"),
    ...(value.allowMissingIdentity === undefined
      ? {}
      : { allowMissingIdentity: value.allowMissingIdentity as boolean }),
  };
}

function objectSchema(
  properties: Record<string, unknown>,
  required: string[] = [],
): Record<string, unknown> {
  return {
    type: "object",
    additionalProperties: false,
    properties,
    ...(required.length ? { required } : {}),
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function requiredString(value: Record<string, unknown>, key: string): string {
  const result = optionalString(value, key);
  if (!result) throw new Error(`${key} is required`);
  return result;
}

function optionalString(value: Record<string, unknown>, key: string): string | undefined {
  const raw = value[key];
  if (typeof raw !== "string") return undefined;
  const result = raw.trim();
  return result || undefined;
}

function toolResult(details: unknown): AgentToolResult {
  return {
    content: [{ type: "text", text: JSON.stringify(details, null, 2) }],
    details,
  };
}

function conflicts(left?: string, right?: string): boolean {
  return Boolean(left && right && left !== right);
}

function exactAmbient(left?: string, right?: string): string | undefined {
  if (conflicts(left, right)) return undefined;
  return left ?? right;
}
