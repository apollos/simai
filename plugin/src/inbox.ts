/**
 * Encrypted inbox delivery.
 *
 * The normal path is the owner-only Unix socket, where the Python service
 * validates the full identity tuple before sealing. If the socket is briefly
 * unavailable, the plugin seals the exact same JSON envelope with the public
 * key in vault.header.json and atomically writes ciphertext to inboxDir. A
 * server-side identity refusal never falls back, so fallback cannot bypass an
 * intentional YAML/config rejection.
 */

import {
  chmodSync,
  closeSync,
  constants,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import * as net from "node:net";
import { randomBytes } from "node:crypto";
import { join } from "node:path";
import sodium from "libsodium-wrappers";
import type { CaptureMode } from "./types.js";

let lastWriteTimestampNs = 0n;

const DEFAULT_MAX_BODY_BYTES = 256 * 1024;
const DEFAULT_MAX_QUEUE_ITEMS = 10_000;
const DEFAULT_MAX_QUEUE_BYTES = 512 * 1024 * 1024;

export interface InboxSubmission {
  binding_id: string;
  channel: string;
  account_id: string;
  sender_key: string;
  conversation_id: string | null;
  is_group: boolean;
  capture_mode: CaptureMode;
  /** Nullable only when session_key is present (mirrors the core schema). */
  message_id: string | null;
  session_key: string | null;
  /** Groups every message of one dictation session (开始记录…记录完毕). */
  dictation_id?: string | null;
  /** Who spoke: the owner (default) or the assistant (dictation context). */
  speaker?: "owner" | "assistant";
  body: string;
}

export type InboxSocketResult = "accepted" | "refused" | "unavailable";

export interface InboxFallbackConfig {
  vaultHeaderPath: string;
  inboxDir: string;
  /** Maximum UTF-8 byte length of one fallback body. */
  maxBodyBytes?: number;
  /** Maximum number of regular *.sealed files after this write. */
  maxQueueItems?: number;
  /** Maximum aggregate bytes of regular *.sealed files after this write. */
  maxQueueBytes?: number;
}

export type InboxSocketSubmitter = (
  socketPath: string,
  item: InboxSubmission,
  timeoutMs: number,
) => Promise<InboxSocketResult>;

export async function submitToInbox(
  socketPath: string,
  item: InboxSubmission,
  timeoutMs = 5000,
): Promise<boolean> {
  return (await submitToInboxDetailed(socketPath, item, timeoutMs)) === "accepted";
}

export async function submitWithEncryptedFallback(
  socketPath: string,
  fallback: InboxFallbackConfig,
  item: InboxSubmission,
  timeoutMs = 5000,
  socketSubmitter: InboxSocketSubmitter = submitToInboxDetailed,
): Promise<boolean> {
  const result = await socketSubmitter(socketPath, item, timeoutMs);
  if (result === "accepted") return true;
  if (result === "refused") return false;
  try {
    await sealDirectToInbox(fallback, item);
    return true;
  } catch {
    return false;
  }
}

export async function submitToInboxDetailed(
  socketPath: string,
  item: InboxSubmission,
  timeoutMs = 5000,
): Promise<InboxSocketResult> {
  return new Promise((resolve) => {
    const socket = net.createConnection(socketPath);
    let settled = false;
    const finish = (result: InboxSocketResult): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      resolve(result);
    };
    const timer = setTimeout(() => finish("unavailable"), timeoutMs);

    let buffer = "";
    socket.on("connect", () => socket.write(`${JSON.stringify(item)}\n`));
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf-8");
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      try {
        const response = JSON.parse(buffer.slice(0, newline)) as { ok?: unknown };
        finish(response.ok === true ? "accepted" : "refused");
      } catch {
        finish("unavailable");
      }
    });
    socket.on("error", () => finish("unavailable"));
    socket.on("end", () => finish("unavailable"));
  });
}

export async function sealDirectToInbox(
  config: InboxFallbackConfig,
  item: InboxSubmission,
): Promise<string> {
  validateSubmission(item);
  const limits = fallbackLimits(config);
  if (Buffer.byteLength(item.body, "utf-8") > limits.maxBodyBytes) {
    throw new Error("fallback body exceeds the UTF-8 byte limit");
  }
  const headerStat = lstatSync(config.vaultHeaderPath);
  if (!headerStat.isFile() || headerStat.isSymbolicLink()) {
    throw new Error("vault header must be a regular file");
  }
  if (typeof process.geteuid === "function" && headerStat.uid !== process.geteuid()) {
    throw new Error("vault header must be owned by the current user");
  }
  if ((headerStat.mode & 0o077) !== 0) {
    throw new Error("vault header must not grant group/world permissions");
  }
  const header = JSON.parse(readFileSync(config.vaultHeaderPath, "utf-8")) as {
    format_version?: unknown;
    sealed_inbox_public_key?: unknown;
  };
  if (header.format_version !== 1 || typeof header.sealed_inbox_public_key !== "string") {
    throw new Error("unsupported or invalid vault header");
  }
  const publicKey = Buffer.from(header.sealed_inbox_public_key, "base64");
  if (publicKey.byteLength !== 32) throw new Error("invalid sealed inbox public key");

  const capturedAt = new Date().toISOString();
  const plaintext = Buffer.from(
    JSON.stringify({
      schema_version: 2,
      binding_id: item.binding_id,
      channel: item.channel,
      account_id: item.account_id,
      sender_key: item.sender_key,
      conversation_id: item.conversation_id,
      is_group: item.is_group,
      message_id: item.message_id || null,
      session_key: item.session_key || null,
      dictation_id: item.dictation_id ?? null,
      speaker: item.speaker ?? "owner",
      captured_at: capturedAt,
      body: item.body,
      capture_mode: item.capture_mode,
    }),
    "utf-8",
  );
  await sodium.ready;
  const ciphertext = Buffer.from(sodium.crypto_box_seal(plaintext, publicKey));

  mkdirSync(config.inboxDir, { recursive: true, mode: 0o700 });
  const inboxStat = lstatSync(config.inboxDir);
  if (!inboxStat.isDirectory() || inboxStat.isSymbolicLink()) {
    throw new Error("inboxDir must be a real directory");
  }
  chmodSync(config.inboxDir, 0o700);

  const queue = inspectSealedQueue(config.inboxDir);
  if (queue.items + 1 > limits.maxQueueItems) {
    throw new Error("encrypted fallback queue item limit reached");
  }
  if (queue.bytes + ciphertext.byteLength > limits.maxQueueBytes) {
    throw new Error("encrypted fallback queue byte limit reached");
  }

  const clockTimestampNs = BigInt(Date.now()) * 1_000_000n;
  lastWriteTimestampNs =
    clockTimestampNs > lastWriteTimestampNs ? clockTimestampNs : lastWriteTimestampNs + 1n;
  const timePart = lastWriteTimestampNs.toString().padStart(20, "0");
  const filename = `${timePart}-${randomBytes(6).toString("hex")}.sealed`;
  const finalPath = join(config.inboxDir, filename);
  const temporaryPath = `${finalPath}.tmp`;
  const fd = openSync(
    temporaryPath,
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL,
    0o600,
  );
  try {
    writeFileSync(fd, ciphertext);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(temporaryPath, finalPath);
  chmodSync(finalPath, 0o600);
  fsyncDirectory(config.inboxDir);
  return finalPath;
}

interface ResolvedFallbackLimits {
  maxBodyBytes: number;
  maxQueueItems: number;
  maxQueueBytes: number;
}

function fallbackLimits(config: InboxFallbackConfig): ResolvedFallbackLimits {
  return {
    maxBodyBytes: safeLimit(config.maxBodyBytes, DEFAULT_MAX_BODY_BYTES, "maxBodyBytes"),
    maxQueueItems: safeLimit(config.maxQueueItems, DEFAULT_MAX_QUEUE_ITEMS, "maxQueueItems"),
    maxQueueBytes: safeLimit(config.maxQueueBytes, DEFAULT_MAX_QUEUE_BYTES, "maxQueueBytes"),
  };
}

function safeLimit(value: number | undefined, fallback: number, name: string): number {
  const resolved = value ?? fallback;
  if (!Number.isSafeInteger(resolved) || resolved < 0) {
    throw new Error(`${name} must be a non-negative safe integer`);
  }
  return resolved;
}

function inspectSealedQueue(inboxDir: string): { items: number; bytes: number } {
  let items = 0;
  let bytes = 0;
  for (const name of readdirSync(inboxDir)) {
    if (!name.endsWith(".sealed")) continue;
    const path = join(inboxDir, name);
    const stat = lstatSync(path);
    if (stat.isSymbolicLink()) {
      throw new Error("encrypted fallback queue must not contain .sealed symlinks");
    }
    if (!stat.isFile()) continue;
    items += 1;
    bytes += stat.size;
    if (!Number.isSafeInteger(bytes)) {
      throw new Error("encrypted fallback queue byte count is unsafe");
    }
  }
  return { items, bytes };
}

function validateSubmission(item: InboxSubmission): void {
  const required = [item.binding_id, item.channel, item.account_id, item.sender_key, item.body];
  if (required.some((value) => typeof value !== "string" || value.length === 0)) {
    throw new Error("invalid inbox submission");
  }
  const hasMessageId = typeof item.message_id === "string" && item.message_id.length > 0;
  const hasSessionKey = typeof item.session_key === "string" && item.session_key.length > 0;
  if (!hasMessageId && !hasSessionKey) {
    throw new Error("message_id and session_key cannot both be empty");
  }
  if (item.message_id !== null && typeof item.message_id !== "string") {
    throw new Error("invalid message id");
  }
  if (typeof item.is_group !== "boolean") throw new Error("invalid group flag");
  if (item.capture_mode !== "passive" && item.capture_mode !== "explicit") {
    throw new Error("invalid capture mode");
  }
  const dictationId = item.dictation_id ?? null;
  if (
    dictationId !== null &&
    (typeof dictationId !== "string" ||
      dictationId.length === 0 ||
      Buffer.byteLength(dictationId, "utf-8") > 128)
  ) {
    throw new Error("invalid dictation id");
  }
  const speaker = item.speaker ?? "owner";
  if (speaker !== "owner" && speaker !== "assistant") {
    throw new Error("invalid speaker");
  }
}

function fsyncDirectory(path: string): void {
  let fd: number | undefined;
  try {
    fd = openSync(path, constants.O_RDONLY);
    fsyncSync(fd);
  } catch {
    // File fsync + atomic rename are already sufficient on platforms that do
    // not permit opening/fsyncing a directory.
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}
