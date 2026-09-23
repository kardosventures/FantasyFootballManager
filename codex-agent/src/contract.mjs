import crypto from "node:crypto";

export function canonicalJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`
    )).join(",")}}`;
  }
  return JSON.stringify(value);
}

export function signPayload(secret, payload) {
  return crypto.createHmac("sha256", secret).update(canonicalJson(payload), "utf8").digest("hex");
}

export function signedEnvelope(secret, payload) {
  return { payload, signature: signPayload(secret, payload) };
}

export function verifyEnvelope(secret, envelope) {
  if (!envelope || typeof envelope !== "object" || !envelope.payload) {
    throw new Error("Invalid signed envelope");
  }
  const supplied = String(envelope.signature ?? "");
  const expected = signPayload(secret, envelope.payload);
  if (supplied.length !== expected.length || !crypto.timingSafeEqual(
    Buffer.from(supplied, "utf8"),
    Buffer.from(expected, "utf8"),
  )) {
    throw new Error("Invalid request signature");
  }
  return envelope.payload;
}

export function validateRequest(payload, config, nowSeconds = Date.now() / 1000) {
  if (payload.version !== 1) throw new Error("Unsupported request version");
  if (!/^[a-f0-9]{64}$/.test(String(payload.request_id ?? ""))) {
    throw new Error("Invalid request id");
  }
  if (!Number.isFinite(payload.created_at) || !Number.isFinite(payload.expires_at)) {
    throw new Error("Invalid request timestamps");
  }
  if (payload.created_at > nowSeconds + 5 || payload.expires_at <= nowSeconds) {
    throw new Error("Request is expired or not yet valid");
  }
  const maxLifetime = payload.purpose === "in_season_management" ? 305 : 95;
  if (payload.expires_at - payload.created_at > maxLifetime) {
    throw new Error("Request lifetime is too long");
  }
  if (payload.model !== config.model) throw new Error("Unexpected Codex model");
  if (payload.reasoning_effort !== config.reasoningEffort) {
    throw new Error("Unexpected reasoning effort");
  }
  if (payload.purpose === "trash_talk" && payload.web_search_enabled !== false) {
    throw new Error("Trash-talk requests must disable web search");
  }
  if (payload.purpose !== "trash_talk" && payload.web_search_enabled !== config.webSearchEnabled) {
    throw new Error("Unexpected web-search setting");
  }
  if (typeof payload.prompt !== "string" || payload.prompt.length < 100) {
    throw new Error("Missing expert prompt");
  }
  if (!payload.schema || typeof payload.schema !== "object") {
    throw new Error("Missing output schema");
  }
  return payload;
}
