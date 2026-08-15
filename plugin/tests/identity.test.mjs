import assert from "node:assert/strict";
import test from "node:test";

import { CorrelationStore, matchBinding } from "../dist/identity.js";

const binding = {
  id: "owner",
  channel: "openclaw-weixin",
  accountId: "bot",
  senderKey: "yu",
  conversationId: "private-chat",
  allowGroup: false,
  passiveCapture: true,
  enabled: true,
};

const envelope = (overrides = {}) => ({
  binding,
  channel: binding.channel,
  accountId: binding.accountId,
  senderKey: binding.senderKey,
  conversationId: binding.conversationId,
  messageId: "m1",
  sessionKey: "s1",
  receivedAt: Date.now(),
  ...overrides,
});

const body = (overrides = {}) => ({
  body: "hello",
  channel: binding.channel,
  senderKey: binding.senderKey,
  conversationId: binding.conversationId,
  isGroup: false,
  messageId: "m1",
  sessionKey: "s1",
  receivedAt: Date.now(),
  ...overrides,
});

test("binding is exact, group-safe, and ambiguity fails closed", () => {
  assert.equal(matchBinding([binding], {
    channel: binding.channel,
    accountId: binding.accountId,
    senderId: binding.senderKey,
    isGroup: false,
  }), null);
  assert.equal(matchBinding([binding], {
    channel: binding.channel,
    accountId: binding.accountId,
    senderId: binding.senderKey,
    conversationId: "other",
    isGroup: false,
  }), null);
  assert.equal(matchBinding([binding], {
    channel: binding.channel,
    accountId: binding.accountId,
    senderId: binding.senderKey,
    conversationId: binding.conversationId,
    isGroup: true,
  }), null);
  assert.equal(matchBinding([binding], {
    channel: binding.channel,
    accountId: binding.accountId,
    senderId: binding.senderKey,
    conversationId: binding.conversationId,
    isGroup: false,
  })?.id, binding.id);
  assert.equal(matchBinding([binding, { ...binding, id: "duplicate" }], {
    channel: binding.channel,
    accountId: binding.accountId,
    senderId: binding.senderKey,
    conversationId: binding.conversationId,
    isGroup: false,
  }), null);
});

test("two-phase correlation works in either event order and returns full identity", () => {
  const first = new CorrelationStore();
  assert.equal(first.addEnvelope(envelope()), null);
  const matched = first.addBody(body({ sessionKey: undefined }));
  assert.deepEqual({ ...matched, receivedAt: 0 }, {
    bindingId: binding.id,
    channel: binding.channel,
    accountId: binding.accountId,
    senderKey: binding.senderKey,
    conversationId: binding.conversationId,
    isGroup: false,
    messageId: "m1",
    sessionKey: "s1",
    captureMode: "passive",
    receivedAt: 0,
    body: "hello",
  });

  const reverse = new CorrelationStore();
  assert.equal(reverse.addBody(body({ body: "world" })), null);
  assert.equal(reverse.addEnvelope(envelope())?.body, "world");
});

test("message/session and every overlapping identity field must agree", () => {
  for (const override of [
    { channel: "other" },
    { senderKey: "attacker" },
    { conversationId: "other" },
    { isGroup: true },
  ]) {
    const store = new CorrelationStore();
    store.addEnvelope(envelope());
    assert.equal(store.addBody(body(override)), null);
  }

  const sessionMismatch = new CorrelationStore();
  sessionMismatch.addEnvelope(envelope({ sessionKey: "typed-session" }));
  assert.equal(sessionMismatch.addBody(body({ sessionKey: "other-session" })), null);

  const sessionIsolation = new CorrelationStore();
  sessionIsolation.addEnvelope(envelope({ binding, conversationId: "conversation-a" }));
  sessionIsolation.addEnvelope(envelope({
    binding: { ...binding, id: "b", conversationId: "conversation-b" },
    conversationId: "conversation-b",
  }));
  assert.equal(sessionIsolation.addBody(body({ conversationId: "conversation-b" }))?.bindingId, "b");
  assert.equal(sessionIsolation.addBody(body({ conversationId: "conversation-a" }))?.bindingId, "owner");

  const missing = new CorrelationStore();
  assert.equal(missing.addEnvelope(envelope({ messageId: undefined })), null);
  assert.equal(missing.addBody(body({ messageId: undefined })), null);
});
