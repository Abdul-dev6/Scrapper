// Run with: npm test
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSON_BODY_LIMIT, readJsonBody } from "../src/lib/http";

const req = (body: BodyInit | null, headers: Record<string, string> = {}) =>
  new Request("http://test/x", { method: "POST", body, headers, ...(body instanceof ReadableStream ? { duplex: "half" } : {}) } as RequestInit);

test("normal JSON bodies are parsed", async () => {
  const r = await readJsonBody(req(JSON.stringify({ a: 1, notes: "سلام".repeat(100) })));
  assert.ok(r.ok && (r.data as { a: number }).a === 1);
});

test("invalid or empty JSON gives null (same as before, so zod returns 400)", async () => {
  const bad = await readJsonBody(req("{not json"));
  assert.ok(bad.ok && bad.data === null);
  const empty = await readJsonBody(req(null));
  assert.ok(empty.ok && empty.data === null);
});

test("a body over the limit is refused with 413 from its Content-Length", async () => {
  const r = await readJsonBody(req("[]", { "content-length": String(JSON_BODY_LIMIT + 1) }));
  assert.ok(!r.ok && r.response.status === 413);
});

test("a body over the limit is refused even without (or with a false) Content-Length", async () => {
  const big = JSON.stringify(Array(200_000).fill("bogus")); // the 1.6 MB attack body measured in the audit
  let pulled = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      const chunk = new TextEncoder().encode(big.slice(pulled, pulled + 4096));
      pulled += 4096;
      if (chunk.byteLength) controller.enqueue(chunk);
      else controller.close();
    },
  });
  const r = await readJsonBody(req(stream, { "content-length": "10" })); // lies about its size
  assert.ok(!r.ok && r.response.status === 413);
  assert.ok(pulled < JSON_BODY_LIMIT * 2, `stopped reading early (read ${pulled} bytes)`);
});

test("exactly at the limit is accepted; custom limits work", async () => {
  const s = JSON.stringify("x".repeat(JSON_BODY_LIMIT - 2)); // quotes make it exactly the limit
  assert.equal(new TextEncoder().encode(s).byteLength, JSON_BODY_LIMIT);
  assert.ok((await readJsonBody(req(s))).ok);
  const r = await readJsonBody(req(JSON.stringify({ notes: "x".repeat(40_000) })), 64 * 1024);
  assert.ok(r.ok);
});
