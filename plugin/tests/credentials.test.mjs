import assert from "node:assert/strict";
import {
  chmodSync,
  mkdtempSync,
  mkdirSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { readOwnerOnlyToken } from "../dist/credentials.js";

test("plugin token must be an owner-only regular file and never a symlink", () => {
  const root = mkdtempSync(join(tmpdir(), "simai-token-"));
  try {
    const tokenPath = join(root, "plugin.token");
    writeFileSync(tokenPath, "x".repeat(32), { mode: 0o600 });
    chmodSync(tokenPath, 0o600);
    assert.equal(readOwnerOnlyToken(tokenPath), "x".repeat(32));

    chmodSync(tokenPath, 0o640);
    assert.throws(() => readOwnerOnlyToken(tokenPath), /group\/world/);
    chmodSync(tokenPath, 0o600);

    const linkPath = join(root, "token-link");
    symlinkSync(tokenPath, linkPath);
    assert.throws(() => readOwnerOnlyToken(linkPath), /symlink/);

    const directoryPath = join(root, "token-dir");
    mkdirSync(directoryPath);
    assert.throws(() => readOwnerOnlyToken(directoryPath), /regular file/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("plugin token must be owned by the current effective user on POSIX", (t) => {
  if (typeof process.geteuid !== "function") {
    t.skip("effective-user ownership checks apply only on POSIX");
    return;
  }
  const root = mkdtempSync(join(tmpdir(), "simai-token-owner-"));
  const realGeteuid = process.geteuid;
  try {
    const tokenPath = join(root, "plugin.token");
    writeFileSync(tokenPath, "x".repeat(32), { mode: 0o600 });
    chmodSync(tokenPath, 0o600);
    process.geteuid = () => realGeteuid() + 1;
    assert.throws(() => readOwnerOnlyToken(tokenPath), /owned by the current user/);
  } finally {
    process.geteuid = realGeteuid;
    rmSync(root, { recursive: true, force: true });
  }
});
