import assert from "node:assert/strict";
import test from "node:test";

import { canonicalJson, signedEnvelope, validateRequest, verifyEnvelope } from "../src/contract.mjs";

const config = {
  model: "gpt-5.6-terra",
  reasoningEffort: "medium",
  webSearchEnabled: true,
};

function request(now = 1_800_000_000) {
  return {
    version: 1,
    request_id: "a".repeat(64),
    created_at: now - 1,
    expires_at: now + 50,
    model: "gpt-5.6-terra",
    reasoning_effort: "medium",
    web_search_enabled: true,
    prompt: "x".repeat(101),
    schema: { type: "object", properties: {} },
  };
}

test("canonical JSON recursively sorts object keys", () => {
  assert.equal(canonicalJson({ z: 1, a: { y: 2, b: 3 } }), '{"a":{"b":3,"y":2},"z":1}');
});

test("signed requests verify and retain their payload", () => {
  const payload = request();
  assert.deepEqual(verifyEnvelope("a-long-enough-test-secret", signedEnvelope("a-long-enough-test-secret", payload)), payload);
});

test("tampered signed requests are rejected", () => {
  const envelope = signedEnvelope("a-long-enough-test-secret", request());
  envelope.payload.model = "different";
  assert.throws(() => verifyEnvelope("a-long-enough-test-secret", envelope), /signature/);
});

test("expired and configuration-mismatched requests are rejected", () => {
  assert.throws(() => validateRequest(request(100), config, 200), /expired/);
  assert.throws(
    () => validateRequest({ ...request(), model: "other" }, config, 1_800_000_000),
    /Unexpected Codex model/,
  );
});

test("in-season analysis has a longer bounded lifetime than draft decisions", () => {
  const longRequest = {
    ...request(),
    purpose: "in_season_management",
    expires_at: 1_800_000_299,
  };
  assert.equal(validateRequest(longRequest, config, 1_800_000_000), longRequest);
  assert.throws(
    () => validateRequest({ ...longRequest, expires_at: 1_800_000_310 }, config, 1_800_000_000),
    /lifetime/,
  );
  assert.throws(
    () => validateRequest({ ...request(), expires_at: 1_800_000_100 }, config, 1_800_000_000),
    /lifetime/,
  );
});

test("trash-talk requests must disable web search", () => {
  const trash = {
    ...request(),
    purpose: "trash_talk",
    web_search_enabled: false,
  };
  assert.equal(validateRequest(trash, config, 1_800_000_000), trash);
  assert.throws(
    () => validateRequest({ ...trash, web_search_enabled: true }, config, 1_800_000_000),
    /disable web search/,
  );
});
