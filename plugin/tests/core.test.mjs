import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import { SimaiCoreClient } from "../dist/core.js";

test("search uses authenticated POST JSON instead of leaking the query in a URL", async () => {
  let observed;
  const server = http.createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => { body += chunk.toString("utf-8"); });
    request.on("end", () => {
      observed = {
        method: request.method,
        url: request.url,
        token: request.headers["x-simai-plugin-token"],
        body: JSON.parse(body),
      };
      response.writeHead(200, { "content-type": "application/json" });
      response.end('{"results":[]}');
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  try {
    const address = server.address();
    const token = "x".repeat(32);
    const result = await new SimaiCoreClient(
      `http://127.0.0.1:${address.port}`,
      token,
      1000,
    ).search("owner", "private query", 7);
    assert.equal(result.ok, true);
    assert.deepEqual(observed, {
      method: "POST",
      url: "/plugin-api/search",
      token,
      body: { binding_id: "owner", q: "private query", limit: 7 },
    });
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("Core HTTP calls have a bounded timeout", async () => {
  const server = http.createServer(() => {
    // Intentionally leave the response open; AbortSignal.timeout must end it.
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  try {
    const address = server.address();
    assert.equal(typeof address, "object");
    const started = Date.now();
    const result = await new SimaiCoreClient(
      `http://127.0.0.1:${address.port}`,
      "x".repeat(32),
      50,
    ).status("owner");
    assert.equal(result.ok, false);
    assert.ok(Date.now() - started < 1000);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});
