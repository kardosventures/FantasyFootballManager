const REQUEST_TIMEOUT_MS = 15_000;

export class AgentApi {
  constructor(config, fetchImpl = fetch) {
    this.baseUrl = config.apiUrl;
    this.secret = config.sharedSecret;
    this.fetchImpl = fetchImpl;
  }

  async request(path, body) {
    const response = await this.fetchImpl(new URL(path, this.baseUrl), {
      method: "POST",
      headers: {
        authorization: `Bearer ${this.secret}`,
        "content-type": "application/json",
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    if (response.status === 204) return null;
    if (!response.ok) throw new Error(`Manager API returned HTTP ${response.status}`);
    return response.json();
  }

  async get(path) {
    const response = await this.fetchImpl(new URL(path, this.baseUrl), {
      method: "GET",
      headers: { authorization: `Bearer ${this.secret}` },
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    if (!response.ok) throw new Error(`Manager API returned HTTP ${response.status}`);
    return response.json();
  }

  heartbeat(payload) {
    return this.request("/internal/agent/heartbeat", payload);
  }

  freeAgentTargets() {
    return this.get("/internal/agent/free-agent-targets");
  }

  lease(agentId, constraints = {}) {
    return this.request("/internal/agent/lease", { agent_id: agentId, ...constraints });
  }

  preflight(commandId, agentId, uiObservation = {}) {
    return this.request(`/internal/agent/commands/${commandId}/preflight`, {
      agent_id: agentId,
      ui_observation: uiObservation,
    });
  }

  qualificationRetry(commandId, agentId) {
    return this.request(`/internal/agent/commands/${commandId}/qualification-retry`, {
      agent_id: agentId,
    });
  }

  report(commandId, payload) {
    return this.request(`/internal/agent/commands/${commandId}/result`, payload);
  }
}
