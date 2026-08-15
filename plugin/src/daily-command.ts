/** Deterministic, content-free worker for OpenClaw command cron. */

import { homedir } from "node:os";
import { SimaiCoreClient } from "./core.js";
import { readOwnerOnlyToken } from "./credentials.js";

async function main(argv: string[]): Promise<number> {
  const options = parseArgs(argv);
  const coreUrl = required(options, "core-url").replace(/\/$/, "");
  const parsedUrl = new URL(coreUrl);
  if (!(["127.0.0.1", "::1", "localhost"] as string[]).includes(parsedUrl.hostname)) {
    throw new Error("--core-url must be loopback-only");
  }
  const tokenPath = required(options, "token-file").replace(/^~(?=\/)/, homedir());
  const token = readOwnerOnlyToken(tokenPath);

  const result = await new SimaiCoreClient(coreUrl, token).runDaily(
    required(options, "binding-id"),
  );
  if (result.locked || result.data?.locked) {
    console.log("思脉处于锁定状态，今日内容仍在加密待确认箱中。请通过 Web 管理端解锁。");
    return 0;
  }
  if (!result.ok || result.data?.failed) {
    console.error("思脉每日整理失败；水位未推进，请检查服务日志后重试。");
    return 1;
  }
  if (!result.data?.notify) {
    // OpenClaw suppresses this canonical token from announce delivery.
    console.log("NO_REPLY");
    return 0;
  }
  const pending = Number(result.data.pending_total ?? 0);
  console.log(`思脉有 ${pending} 条待确认候选，请在 Web 管理端处理。`);
  return 0;
}

function parseArgs(argv: string[]): Map<string, string> {
  const result = new Map<string, string>();
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined) {
      throw new Error("usage: daily-command --core-url URL --token-file PATH --binding-id ID");
    }
    result.set(key.slice(2), value);
  }
  return result;
}

function required(options: Map<string, string>, key: string): string {
  const value = options.get(key)?.trim();
  if (!value) throw new Error(`--${key} is required`);
  return value;
}

main(process.argv.slice(2))
  .then((code) => {
    process.exitCode = code;
  })
  .catch((error) => {
    const message = error instanceof Error ? error.message : "unexpected error";
    console.error(`simai daily command failed: ${message}`);
    process.exitCode = 1;
  });
