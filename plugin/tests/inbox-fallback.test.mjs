import assert from "node:assert/strict";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import sodium from "libsodium-wrappers";

import {
  sealDirectToInbox,
  submitWithEncryptedFallback,
} from "../dist/inbox.js";

const item = {
  binding_id: "yu_weixin",
  channel: "openclaw-weixin",
  account_id: "bot-account",
  sender_key: "yu-user",
  conversation_id: "private-chat",
  is_group: false,
  capture_mode: "passive",
  message_id: "message-1",
  session_key: "session-1",
  body: "只应以密文落盘",
};

async function fixture() {
  await sodium.ready;
  const root = mkdtempSync(join(tmpdir(), "simai-fallback-"));
  const headerPath = join(root, "vault.header.json");
  const inboxDir = join(root, "inbox");
  const keypair = sodium.crypto_box_keypair();
  writeFileSync(headerPath, JSON.stringify({
    format_version: 1,
    sealed_inbox_public_key: Buffer.from(keypair.publicKey).toString("base64"),
  }), { mode: 0o600 });
  chmodSync(headerPath, 0o600);
  return { root, headerPath, inboxDir, keypair };
}

test("direct fallback writes a 0600 schema-v2 sealed box with the full tuple", async () => {
  const f = await fixture();
  try {
    const path = await sealDirectToInbox(
      { vaultHeaderPath: f.headerPath, inboxDir: f.inboxDir },
      item,
    );
    assert.equal(statSync(path).mode & 0o777, 0o600);
    assert.equal(statSync(f.inboxDir).mode & 0o777, 0o700);
    const opened = sodium.crypto_box_seal_open(
      readFileSync(path),
      f.keypair.publicKey,
      f.keypair.privateKey,
    );
    const envelope = JSON.parse(Buffer.from(opened).toString("utf-8"));
    assert.equal(envelope.schema_version, 2);
    assert.equal(envelope.binding_id, item.binding_id);
    assert.equal(envelope.channel, item.channel);
    assert.equal(envelope.account_id, item.account_id);
    assert.equal(envelope.sender_key, item.sender_key);
    assert.equal(envelope.conversation_id, item.conversation_id);
    assert.equal(envelope.is_group, item.is_group);
    assert.equal(envelope.capture_mode, item.capture_mode);
    assert.equal(envelope.body, item.body);
    assert.ok(!readFileSync(path).includes(Buffer.from(item.body)));
  } finally {
    rmSync(f.root, { recursive: true, force: true });
  }
});

test("direct fallback enforces the body UTF-8 byte limit", async () => {
  const f = await fixture();
  try {
    await assert.rejects(
      sealDirectToInbox(
        {
          vaultHeaderPath: f.headerPath,
          inboxDir: f.inboxDir,
          maxBodyBytes: 2,
        },
        { ...item, body: "你" },
      ),
      /UTF-8 byte limit/,
    );
  } finally {
    rmSync(f.root, { recursive: true, force: true });
  }
});

test("direct fallback accepts only an owner-only vault header", async (t) => {
  const permissionFixture = await fixture();
  try {
    chmodSync(permissionFixture.headerPath, 0o640);
    await assert.rejects(
      sealDirectToInbox(
        {
          vaultHeaderPath: permissionFixture.headerPath,
          inboxDir: permissionFixture.inboxDir,
        },
        item,
      ),
      /vault header must not grant group\/world permissions/,
    );
  } finally {
    rmSync(permissionFixture.root, { recursive: true, force: true });
  }

  if (typeof process.geteuid !== "function") {
    t.diagnostic("effective-user ownership checks apply only on POSIX");
    return;
  }
  const ownerFixture = await fixture();
  const realGeteuid = process.geteuid;
  try {
    process.geteuid = () => realGeteuid() + 1;
    await assert.rejects(
      sealDirectToInbox(
        { vaultHeaderPath: ownerFixture.headerPath, inboxDir: ownerFixture.inboxDir },
        item,
      ),
      /vault header must be owned by the current user/,
    );
  } finally {
    process.geteuid = realGeteuid;
    rmSync(ownerFixture.root, { recursive: true, force: true });
  }
});

test("direct fallback enforces queue item and aggregate byte limits", async () => {
  const itemFixture = await fixture();
  try {
    await sealDirectToInbox(
      {
        vaultHeaderPath: itemFixture.headerPath,
        inboxDir: itemFixture.inboxDir,
        maxQueueItems: 1,
      },
      item,
    );
    await assert.rejects(
      sealDirectToInbox(
        {
          vaultHeaderPath: itemFixture.headerPath,
          inboxDir: itemFixture.inboxDir,
          maxQueueItems: 1,
        },
        { ...item, message_id: "message-2" },
      ),
      /queue item limit/,
    );
    assert.equal(
      readdirSync(itemFixture.inboxDir).filter((name) => name.endsWith(".sealed")).length,
      1,
    );
  } finally {
    rmSync(itemFixture.root, { recursive: true, force: true });
  }

  const byteFixture = await fixture();
  try {
    const firstPath = await sealDirectToInbox(
      { vaultHeaderPath: byteFixture.headerPath, inboxDir: byteFixture.inboxDir },
      item,
    );
    const firstSize = statSync(firstPath).size;
    await assert.rejects(
      sealDirectToInbox(
        {
          vaultHeaderPath: byteFixture.headerPath,
          inboxDir: byteFixture.inboxDir,
          maxQueueItems: 2,
          maxQueueBytes: firstSize,
        },
        { ...item, message_id: "message-2" },
      ),
      /queue byte limit/,
    );
  } finally {
    rmSync(byteFixture.root, { recursive: true, force: true });
  }
});

test("queue accounting ignores non-files and rejects .sealed symlinks", async () => {
  const f = await fixture();
  try {
    mkdirSync(f.inboxDir, { mode: 0o700 });
    mkdirSync(join(f.inboxDir, "not-an-item.sealed"));
    await sealDirectToInbox(
      {
        vaultHeaderPath: f.headerPath,
        inboxDir: f.inboxDir,
        maxQueueItems: 1,
      },
      item,
    );

    writeFileSync(join(f.inboxDir, "target.bin"), "not a queue item");
    symlinkSync(join(f.inboxDir, "target.bin"), join(f.inboxDir, "hostile.sealed"));
    await assert.rejects(
      sealDirectToInbox(
        { vaultHeaderPath: f.headerPath, inboxDir: f.inboxDir },
        { ...item, message_id: "message-2" },
      ),
      /must not contain \.sealed symlinks/,
    );
  } finally {
    rmSync(f.root, { recursive: true, force: true });
  }
});

test("transport failure falls back, but an explicit ingress refusal does not", async () => {
  const f = await fixture();
  try {
    assert.equal(await submitWithEncryptedFallback(
      join(f.root, "missing.sock"),
      { vaultHeaderPath: f.headerPath, inboxDir: f.inboxDir },
      item,
      100,
    ), true);

    const refusedDir = join(f.root, "must-not-exist");
    assert.equal(await submitWithEncryptedFallback(
      "unused.sock",
      { vaultHeaderPath: f.headerPath, inboxDir: refusedDir },
      item,
      100,
      async () => "refused",
    ), false);
    assert.throws(() => statSync(refusedDir));
  } finally {
    rmSync(f.root, { recursive: true, force: true });
  }
});
