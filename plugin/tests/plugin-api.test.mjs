import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import sodium from "libsodium-wrappers";

import plugin, { resetSimaiSharedState } from "../dist/index.js";

// Correlation state is intentionally process-global (the gateway registers
// the plugin several times per process); isolate it between tests.
test.beforeEach(() => resetSimaiSharedState());

const binding = {
  id: "yu_weixin",
  channel: "openclaw-weixin",
  accountId: "bot-account",
  senderKey: "yu-user",
  conversationId: "yu-private-chat",
  allowGroup: false,
  passiveCapture: true,
  enabled: true,
};

function pluginConfig(root, socketPath) {
  return {
    coreUrl: "http://127.0.0.1:1",
    coreTokenFile: join(root, "plugin.token"),
    inboxSocket: socketPath,
    vaultHeaderPath: join(root, "vault.header.json"),
    inboxDir: join(root, "inbox"),
    bindings: [binding],
  };
}

function mockApi(config, mode = "full") {
  const typedHooks = new Map();
  const internalHooks = new Map();
  const tools = [];
  const api = {
    id: "simai",
    registrationMode: mode,
    pluginConfig: config,
    logger: { info() {}, warn() {}, error() {} },
    resolvePath(value) { return value; },
    on(name, handler) { typedHooks.set(name, handler); },
    registerHook(name, handler, options) { internalHooks.set(name, { handler, options }); },
    registerTool(factory, options) { tools.push({ factory, options }); },
  };
  return { api, typedHooks, internalHooks, tools };
}

function readEnvelopes(root, keypair) {
  const inboxDir = join(root, "inbox");
  return readdirSync(inboxDir)
    .filter((name) => name.endsWith(".sealed"))
    .map((name) => sodium.crypto_box_seal_open(
      readFileSync(join(inboxDir, name)),
      keypair.publicKey,
      keypair.privateKey,
    ))
    .map((plaintext) => JSON.parse(Buffer.from(plaintext).toString("utf-8")));
}

test("2026.7.1 hooks correlate exact identity and tools use factory/execute", async () => {
  const root = mkdtempSync(join(process.cwd(), ".simai-plugin-api-"));
  const socketPath = join(root, "inbox.sock");
  try {
    await sodium.ready;
    const keypair = sodium.crypto_box_keypair();
    writeFileSync(join(root, "vault.header.json"), JSON.stringify({
      format_version: 1,
      sealed_inbox_public_key: Buffer.from(keypair.publicKey).toString("base64"),
    }), { mode: 0o600 });
    writeFileSync(join(root, "plugin.token"), "x".repeat(32), { mode: 0o600 });
    const runtime = mockApi(pluginConfig(root, socketPath));
    plugin.register(runtime.api);

    assert.equal(typeof runtime.typedHooks.get("message_received"), "function");
    assert.equal(typeof runtime.internalHooks.get("message:preprocessed")?.handler, "function");
    assert.equal(runtime.tools.length, 7);
    assert.ok(runtime.tools.every(({ factory, options }) =>
      typeof factory === "function" && options.names.length === 1));

    const receive = runtime.typedHooks.get("message_received");
    const preprocess = runtime.internalHooks.get("message:preprocessed").handler;
    await receive(
      { from: binding.senderKey, content: "[Audio]", messageId: "m1", sessionKey: "s1" },
      {
        channelId: binding.channel,
        accountId: binding.accountId,
        conversationId: binding.conversationId,
        senderId: binding.senderKey,
        messageId: "m1",
        sessionKey: "s1",
      },
    );
    await preprocess({
      type: "message",
      action: "preprocessed",
      messages: [],
      context: {
        bodyForAgent: "语音转写后的思想",
        from: binding.senderKey,
        senderId: binding.senderKey,
        channelId: binding.channel,
        conversationId: binding.conversationId,
        messageId: "m1",
        isGroup: false,
      },
    });
    let received = readEnvelopes(root, keypair);
    const passive = received.find((item) => item.body === "语音转写后的思想");
    assert.deepEqual({
      binding_id: passive.binding_id,
      channel: passive.channel,
      account_id: passive.account_id,
      sender_key: passive.sender_key,
      conversation_id: passive.conversation_id,
      is_group: passive.is_group,
      capture_mode: passive.capture_mode,
      message_id: passive.message_id,
      session_key: passive.session_key,
      body: passive.body,
    }, {
      binding_id: binding.id,
      channel: binding.channel,
      account_id: binding.accountId,
      sender_key: binding.senderKey,
      conversation_id: binding.conversationId,
      is_group: false,
      capture_mode: "passive",
      message_id: "m1",
      session_key: "s1",
      body: "语音转写后的思想",
    });

    await receive(
      {
        from: binding.senderKey,
        content: "我在开车，接下来只记录",
        messageId: "m2",
        sessionKey: "s2",
      },
      {
        channelId: binding.channel,
        accountId: binding.accountId,
        conversationId: binding.conversationId,
        senderId: binding.senderKey,
        messageId: "m2",
        sessionKey: "s2",
      },
    );
    await preprocess({
      type: "message",
      action: "preprocessed",
      sessionKey: "s2",
      messages: [],
      context: {
        bodyForAgent: "我在开车，接下来只记录",
        from: binding.senderKey,
        senderId: binding.senderKey,
        channelId: binding.channel,
        conversationId: binding.conversationId,
        messageId: "m2",
        isGroup: false,
      },
    });

    await receive(
      {
        from: binding.senderKey,
        content: "这是驾驶期间直接说出的想法",
        messageId: "m2-note",
        sessionKey: "s2-note",
      },
      {
        channelId: binding.channel,
        accountId: binding.accountId,
        conversationId: binding.conversationId,
        senderId: binding.senderKey,
        messageId: "m2-note",
        sessionKey: "s2-note",
      },
    );
    await preprocess({
      type: "message",
      action: "preprocessed",
      messages: [],
      context: {
        bodyForAgent: "这是驾驶期间直接说出的想法",
        from: binding.senderKey,
        senderId: binding.senderKey,
        channelId: binding.channel,
        conversationId: binding.conversationId,
        messageId: "m2-note",
        isGroup: false,
      },
    });
    received = readEnvelopes(root, keypair);
    const directDriving = received.find(
      (entry) => entry.body === "这是驾驶期间直接说出的想法",
    );
    assert.equal(directDriving.capture_mode, "explicit");
    assert.equal(
      received.some((entry) => entry.body === "我在开车，接下来只记录"),
      false,
    );

    const captureRegistration = runtime.tools.find(({ options }) =>
      options.names.includes("simai_capture"));
    const tool = captureRegistration.factory({
      sessionKey: "s2",
      messageChannel: binding.channel,
      agentAccountId: binding.accountId,
      requesterSenderId: binding.senderKey,
      senderIsOwner: true,
      deliveryContext: {
        channel: binding.channel,
        accountId: binding.accountId,
        to: binding.conversationId,
      },
    });
    assert.equal(typeof tool.execute, "function");
    const result = await tool.execute("call-1", { text: "驾驶中的灵感" });
    assert.equal(result.details.status, "已进入加密待确认箱");
    received = readEnvelopes(root, keypair);
    const driving = received.find((item) => item.body === "驾驶中的灵感");
    assert.equal(driving.capture_mode, "explicit");
    assert.equal(driving.channel, binding.channel);
    assert.equal(driving.account_id, binding.accountId);
    assert.equal(driving.sender_key, binding.senderKey);
    assert.equal(driving.conversation_id, binding.conversationId);
    assert.equal(driving.is_group, false);

    const deniedTool = captureRegistration.factory({ sessionKey: "s2" });
    const denied = await deniedTool.execute("call-2", { text: "must not pass" });
    assert.match(denied.details.error, /not whitelisted/);

    // Leave driving mode, correlate another exact turn, then prove that a Core
    // connection failure is encrypted as explicit instead of being lost.
    await receive(
      { from: binding.senderKey, content: "不开车了", messageId: "m3", sessionKey: "s3" },
      {
        channelId: binding.channel,
        accountId: binding.accountId,
        conversationId: binding.conversationId,
        senderId: binding.senderKey,
        messageId: "m3",
        sessionKey: "s3",
      },
    );
    await preprocess({
      type: "message",
      action: "preprocessed",
      messages: [],
      context: {
        bodyForAgent: "不开车了",
        from: binding.senderKey,
        senderId: binding.senderKey,
        channelId: binding.channel,
        conversationId: binding.conversationId,
        messageId: "m3",
        isGroup: false,
      },
    });
    await receive(
      { from: binding.senderKey, content: "普通聊天", messageId: "m4", sessionKey: "s4" },
      {
        channelId: binding.channel,
        accountId: binding.accountId,
        conversationId: binding.conversationId,
        senderId: binding.senderKey,
        messageId: "m4",
        sessionKey: "s4",
      },
    );
    await preprocess({
      type: "message",
      action: "preprocessed",
      messages: [],
      context: {
        bodyForAgent: "普通聊天",
        from: binding.senderKey,
        senderId: binding.senderKey,
        channelId: binding.channel,
        conversationId: binding.conversationId,
        messageId: "m4",
        isGroup: false,
      },
    });
    const unavailableTool = captureRegistration.factory({
      sessionKey: "s4",
      messageChannel: binding.channel,
      agentAccountId: binding.accountId,
      requesterSenderId: binding.senderKey,
      deliveryContext: {
        channel: binding.channel,
        accountId: binding.accountId,
        to: binding.conversationId,
      },
    });
    const unavailable = await unavailableTool.execute("call-3", { text: "Core 故障时也不能丢" });
    assert.match(unavailable.details.status, /加密放入待确认箱/);
    received = readEnvelopes(root, keypair);
    assert.equal(received.find((item) => item.body === "普通聊天").capture_mode, "passive");
    assert.equal(received.find((item) => item.body === "Core 故障时也不能丢").capture_mode, "explicit");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("strict binding authorizes despite host owner=false when identity fully matches", async () => {
  const root = mkdtempSync(join(process.cwd(), ".simai-plugin-owner-"));
  try {
    await sodium.ready;
    const keypair = sodium.crypto_box_keypair();
    writeFileSync(join(root, "vault.header.json"), JSON.stringify({
      format_version: 1,
      sealed_inbox_public_key: Buffer.from(keypair.publicKey).toString("base64"),
    }), { mode: 0o600 });
    writeFileSync(join(root, "plugin.token"), "x".repeat(32), { mode: 0o600 });
    const runtime = mockApi(pluginConfig(root, join(root, "inbox.sock")));
    plugin.register(runtime.api);

    const receive = runtime.typedHooks.get("message_received");
    const preprocess = runtime.internalHooks.get("message:preprocessed").handler;
    await receive(
      { from: binding.senderKey, content: "想法", messageId: "o1", sessionKey: "so1" },
      {
        channelId: binding.channel,
        accountId: binding.accountId,
        conversationId: binding.conversationId,
        senderId: binding.senderKey,
        messageId: "o1",
        sessionKey: "so1",
      },
    );
    await preprocess({
      type: "message",
      action: "preprocessed",
      sessionKey: "so1",
      messages: [],
      context: {
        bodyForAgent: "想法",
        from: binding.senderKey,
        senderId: binding.senderKey,
        channelId: binding.channel,
        conversationId: binding.conversationId,
        messageId: "o1",
        isGroup: false,
      },
    });

    // Real weixin tool context: owner=false, requester absent, but channel/
    // account/delivery target all pin the bound private conversation.
    const captureRegistration = runtime.tools.find(({ options }) =>
      options.names.includes("simai_capture"));
    const tool = captureRegistration.factory({
      sessionKey: "so1",
      messageChannel: binding.channel,
      agentAccountId: binding.accountId,
      senderIsOwner: false,
      deliveryContext: { to: binding.conversationId },
    });
    const result = await tool.execute("call-owner-1", { text: "微信想法" });
    assert.match(JSON.stringify(result.details), /待确认箱/);

    // A mismatching delivery target must still be rejected.
    const wrongTool = captureRegistration.factory({
      sessionKey: "so1",
      messageChannel: binding.channel,
      agentAccountId: binding.accountId,
      senderIsOwner: false,
      deliveryContext: { to: "someone-else" },
    });
    const denied = await wrongTool.execute("call-owner-2", { text: "微信想法" });
    assert.match(JSON.stringify(denied.details), /not whitelisted/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("webchat owner binding captures identity-less payloads and authorizes tools", async () => {
  const root = mkdtempSync(join(process.cwd(), ".simai-plugin-webchat-"));
  try {
    await sodium.ready;
    const keypair = sodium.crypto_box_keypair();
    writeFileSync(join(root, "vault.header.json"), JSON.stringify({
      format_version: 1,
      sealed_inbox_public_key: Buffer.from(keypair.publicKey).toString("base64"),
    }), { mode: 0o600 });
    writeFileSync(join(root, "plugin.token"), "x".repeat(32), { mode: 0o600 });
    const webBinding = {
      id: "local_web",
      channel: "webchat",
      accountId: "local",
      senderKey: "local-owner",
      conversationId: "local-web",
      allowGroup: false,
      passiveCapture: true,
      enabled: true,
      allowMissingIdentity: true,
    };
    const config = {
      ...pluginConfig(root, join(root, "inbox.sock")),
      bindings: [webBinding],
    };
    const runtime = mockApi(config);
    plugin.register(runtime.api);

    const receive = runtime.typedHooks.get("message_received");
    const preprocess = runtime.internalHooks.get("message:preprocessed").handler;
    // Dashboard payload observed in the real host: identity fields all absent.
    await receive(
      { from: "", content: "测试一下", messageId: "w1", sessionKey: "ws1" },
      { channelId: "webchat", messageId: "w1", sessionKey: "ws1" },
    );
    await preprocess({
      type: "message",
      action: "preprocessed",
      sessionKey: "ws1",
      messages: [],
      context: {
        bodyForAgent: "测试一下",
        from: "",
        channelId: "webchat",
        messageId: "w1",
        isGroup: false,
      },
    });
    const received = readEnvelopes(root, keypair);
    const item = received.find((entry) => entry.body === "测试一下");
    assert.equal(item.binding_id, "local_web");
    assert.equal(item.account_id, "local");
    assert.equal(item.sender_key, "local-owner");
    assert.equal(item.conversation_id, "local-web");
    assert.equal(item.capture_mode, "passive");

    // Hosts can replay both hooks for the same message; it must seal once.
    await receive(
      { from: "", content: "测试一下", messageId: "w1", sessionKey: "ws1" },
      { channelId: "webchat", messageId: "w1", sessionKey: "ws1" },
    );
    await preprocess({
      type: "message",
      action: "preprocessed",
      sessionKey: "ws1",
      messages: [],
      context: {
        bodyForAgent: "测试一下",
        from: "",
        channelId: "webchat",
        messageId: "w1",
        isGroup: false,
      },
    });
    assert.equal(
      readEnvelopes(root, keypair).filter((entry) => entry.body === "测试一下").length,
      1,
    );

    // Tool context in the real host is equally identity-less for webchat.
    const captureRegistration = runtime.tools.find(({ options }) =>
      options.names.includes("simai_capture"));
    const tool = captureRegistration.factory({
      sessionKey: "ws1",
      messageChannel: "webchat",
      senderIsOwner: true,
    });
    const result = await tool.execute("call-web-1", { text: "网页想法" });
    // Core is unreachable in this test, so the encrypted fallback answers.
    assert.match(JSON.stringify(result.details), /待确认箱/);

    // Identity-less owner channels still require the host owner flag.
    const nonOwnerTool = captureRegistration.factory({
      sessionKey: "ws1",
      messageChannel: "webchat",
      senderIsOwner: false,
    });
    const deniedNonOwner = await nonOwnerTool.execute("call-web-2", { text: "网页想法" });
    assert.match(JSON.stringify(deniedNonOwner.details), /not whitelisted/);

    // A strict binding must still reject identity-less payloads.
    const strictRuntime = mockApi({
      ...pluginConfig(root, join(root, "inbox.sock")),
      bindings: [{ ...webBinding, id: "strict_web", allowMissingIdentity: false }],
    });
    plugin.register(strictRuntime.api);
    const strictReceive = strictRuntime.typedHooks.get("message_received");
    const strictPreprocess = strictRuntime.internalHooks.get("message:preprocessed").handler;
    await strictReceive(
      { from: "", content: "不该被捕获", messageId: "w2", sessionKey: "ws2" },
      { channelId: "webchat", messageId: "w2", sessionKey: "ws2" },
    );
    await strictPreprocess({
      type: "message",
      action: "preprocessed",
      sessionKey: "ws2",
      messages: [],
      context: {
        bodyForAgent: "不该被捕获",
        from: "",
        channelId: "webchat",
        messageId: "w2",
        isGroup: false,
      },
    });
    assert.equal(
      readEnvelopes(root, keypair).some((entry) => entry.body === "不该被捕获"),
      false,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("probeMode logs identity metadata only and never captures", async () => {
  const root = mkdtempSync(join(process.cwd(), ".simai-plugin-probe-"));
  try {
    const config = { ...pluginConfig(root, join(root, "inbox.sock")), probeMode: true };
    const logged = [];
    const runtime = mockApi(config);
    runtime.api.logger = {
      info(message) { logged.push(message); },
      warn() {},
      error() {},
    };
    plugin.register(runtime.api);

    const receive = runtime.typedHooks.get("message_received");
    const preprocess = runtime.internalHooks.get("message:preprocessed").handler;
    const secret = "这句正文绝不能出现在任何日志里";
    await receive(
      { from: binding.senderKey, content: secret, messageId: "p1", sessionKey: "ps1" },
      {
        channelId: binding.channel,
        accountId: binding.accountId,
        conversationId: binding.conversationId,
        senderId: binding.senderKey,
        messageId: "p1",
        sessionKey: "ps1",
      },
    );
    await preprocess({
      type: "message",
      action: "preprocessed",
      sessionKey: "ps1",
      messages: [],
      context: {
        bodyForAgent: "[Audio] placeholder body",
        from: binding.senderKey,
        senderId: binding.senderKey,
        channelId: binding.channel,
        conversationId: binding.conversationId,
        messageId: "p1",
        isGroup: false,
      },
    });

    const probeLines = logged.filter((line) => line.includes("simai[probe]"));
    assert.equal(probeLines.length, 2);
    const receivedLine = JSON.parse(probeLines[0].replace(/^.*message_received /, ""));
    assert.equal(receivedLine.channelId, binding.channel);
    assert.equal(receivedLine.accountId, binding.accountId);
    assert.equal(receivedLine.senderId, binding.senderKey);
    assert.equal(receivedLine.messageId, "p1");
    assert.equal(receivedLine.hasSessionKey, true);
    assert.equal(receivedLine.contentLength, secret.length);
    const preprocessedLine = JSON.parse(probeLines[1].replace(/^.*message_preprocessed /, ""));
    assert.equal(preprocessedLine.isGroup, false);
    assert.equal(preprocessedLine.bodyLooksLikeMediaPlaceholder, true);
    assert.equal(preprocessedLine.bodyLength, "[Audio] placeholder body".length);

    assert.ok(logged.every((line) => !line.includes(secret)));
    assert.ok(logged.every((line) => !line.includes("placeholder body")));
    assert.throws(() => readdirSync(join(root, "inbox")), /ENOENT/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("tool-discovery registers declared factories without live hooks or token reads", () => {
  const root = join(process.cwd(), ".simai-plugin-does-not-need-to-exist");
  const runtime = mockApi(pluginConfig(root, join(root, "missing.sock")), "tool-discovery");
  plugin.register(runtime.api);
  assert.equal(runtime.typedHooks.size, 0);
  assert.equal(runtime.internalHooks.size, 0);
  assert.deepEqual(
    runtime.tools.flatMap(({ options }) => options.names).sort(),
    [
      "simai_capture",
      "simai_confirm_candidate",
      "simai_get_node",
      "simai_list_candidates",
      "simai_query",
      "simai_search",
      "simai_status",
    ],
  );
});
