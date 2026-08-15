/**
 * Exact source binding plus two-phase OpenClaw message correlation.
 *
 * `message_received(event, ctx)` is the authoritative account/sender envelope.
 * The internal `message:preprocessed` hook supplies the final ASR/media-aware
 * body and group flag. Correlation uses messageId plus channel/sender/
 * conversation. If both halves expose a sessionKey it must also agree, but the
 * internal hook is allowed to omit it.
 */

import type { CaptureMode, SimaiBinding, VerifiedIdentity } from "./types.js";

export interface BindingIdentityInput {
  channel?: string;
  accountId?: string;
  senderId?: string;
  from?: string;
  conversationId?: string | null;
  isGroup?: boolean;
}

export interface MatchBindingOptions {
  /** Phase one does not expose a reliable group flag; phase two must verify it. */
  allowUnknownGroup?: boolean;
}

export interface IdentityEnvelope {
  binding: SimaiBinding;
  channel: string;
  accountId: string;
  senderKey: string;
  conversationId: string | null;
  messageId?: string;
  sessionKey?: string;
  receivedAt: number;
}

export interface PendingBody {
  body: string;
  channel?: string;
  senderKey?: string;
  conversationId?: string | null;
  isGroup?: boolean;
  messageId?: string;
  sessionKey?: string;
  receivedAt: number;
}

export interface MatchedMessage extends VerifiedIdentity {
  body: string;
}

/** senderKey = trusted senderId when present, otherwise event.from. */
export function normalizeSenderKey(
  input: { senderId?: string; from?: string },
): string | undefined {
  const value = input.senderId ?? input.from;
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

/**
 * Exact whitelist match. Ambiguous duplicate bindings fail closed instead of
 * silently selecting whichever happens to appear first.
 */
export function matchBinding(
  bindings: SimaiBinding[],
  input: BindingIdentityInput,
  options: MatchBindingOptions = {},
): SimaiBinding | null {
  const senderKey = normalizeSenderKey(input);
  if (!input.channel || !input.accountId || !senderKey) return null;
  if (typeof input.isGroup !== "boolean" && !options.allowUnknownGroup) return null;

  const matches = bindings.filter((binding) => {
    if (!binding.enabled) return false;
    if (binding.channel !== input.channel) return false;
    if (binding.accountId !== input.accountId) return false;
    if (binding.senderKey !== senderKey) return false;
    if (
      binding.conversationId !== undefined &&
      binding.conversationId !== (input.conversationId ?? undefined)
    ) {
      return false;
    }
    if (input.isGroup === true && !binding.allowGroup) return false;
    return true;
  });
  return matches.length === 1 ? matches[0] : null;
}

const DEFAULT_WINDOW_MS = 5 * 60 * 1000;

/** Bidirectional upsert store for out-of-order hook delivery. */
export class CorrelationStore {
  private readonly envelopes = new Map<string, IdentityEnvelope>();
  private readonly bodies = new Map<string, PendingBody>();

  constructor(private readonly windowMs = DEFAULT_WINDOW_MS) {
    if (!Number.isSafeInteger(windowMs) || windowMs < 1_000) {
      throw new Error("correlation window must be an integer >= 1000 ms");
    }
  }

  addEnvelope(envelope: IdentityEnvelope): MatchedMessage | null {
    this.gc();
    const key = correlationKey(envelope);
    if (!key) return null;
    const body = this.bodies.get(key);
    if (body) {
      this.bodies.delete(key);
      return combine(envelope, body);
    }
    this.envelopes.set(key, envelope);
    return null;
  }

  addBody(body: PendingBody): MatchedMessage | null {
    this.gc();
    const key = correlationKey(body);
    if (!key) return null;
    const envelope = this.envelopes.get(key);
    if (envelope) {
      this.envelopes.delete(key);
      return combine(envelope, body);
    }
    this.bodies.set(key, body);
    return null;
  }

  private gc(): void {
    const cutoff = Date.now() - this.windowMs;
    for (const [key, value] of this.envelopes) {
      if (value.receivedAt < cutoff) this.envelopes.delete(key);
    }
    for (const [key, value] of this.bodies) {
      if (value.receivedAt < cutoff) this.bodies.delete(key);
    }
  }
}

function combine(envelope: IdentityEnvelope, body: PendingBody): MatchedMessage | null {
  if (!envelope.messageId || body.messageId !== envelope.messageId) return null;
  if (envelope.sessionKey && body.sessionKey && body.sessionKey !== envelope.sessionKey) return null;
  if (body.channel !== envelope.channel) return null;
  if (body.senderKey !== envelope.senderKey) return null;
  if ((body.conversationId ?? null) !== envelope.conversationId) return null;
  if (typeof body.isGroup !== "boolean") return null;
  if (body.isGroup && !envelope.binding.allowGroup) return null;

  const captureMode: CaptureMode = "passive";
  return {
    bindingId: envelope.binding.id,
    channel: envelope.channel,
    accountId: envelope.accountId,
    senderKey: envelope.senderKey,
    conversationId: envelope.conversationId,
    isGroup: body.isGroup,
    messageId: envelope.messageId,
    sessionKey: envelope.sessionKey ?? body.sessionKey ?? null,
    captureMode,
    receivedAt: Math.max(envelope.receivedAt, body.receivedAt),
    body: body.body,
  };
}

function correlationKey(input: {
  messageId?: string;
  channel?: string;
  senderKey?: string;
  conversationId?: string | null;
}): string | null {
  if (!input.messageId || !input.channel || !input.senderKey) return null;
  return JSON.stringify([
    input.messageId,
    input.channel,
    input.senderKey,
    input.conversationId ?? null,
  ]);
}
