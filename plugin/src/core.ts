/**
 * HTTP client for the Simai core service plugin API (loopback only).
 * Returns structured results; a locked vault surfaces as { locked: true }.
 */

export interface CoreResult<T> {
  ok: boolean;
  locked?: boolean;
  data?: T;
  error?: string;
}

export class SimaiCoreClient {
  constructor(
    private readonly baseUrl: string,
    private readonly token: string,
    private readonly timeoutMs = 120_000,
  ) {}

  private async request<T>(
    method: "GET" | "POST",
    path: string,
    body?: unknown,
    timeoutMs = this.timeoutMs,
  ): Promise<CoreResult<T>> {
    try {
      const res = await fetch(this.baseUrl + path, {
        method,
        headers: {
          "Content-Type": "application/json",
          "X-Simai-Plugin-Token": this.token,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (res.status === 423) return { ok: false, locked: true };
      const data = (await res.json().catch(() => ({}))) as T & { detail?: string };
      if (!res.ok) return { ok: false, error: data?.detail ?? `HTTP ${res.status}` };
      return { ok: true, data };
    } catch (err) {
      return { ok: false, error: err instanceof Error ? err.message : "network error" };
    }
  }

  capture(bindingId: string, text: string, messageId?: string) {
    return this.request<{ cards: unknown[] }>("POST", "/plugin-api/capture", {
      binding_id: bindingId,
      text,
      message_id: messageId,
    });
  }

  listCandidates(bindingId: string) {
    return this.request<{ candidates: unknown[] }>(
      "GET",
      `/plugin-api/candidates?binding_id=${encodeURIComponent(bindingId)}`,
    );
  }

  decideCandidate(
    bindingId: string,
    candidateId: string,
    decision: "confirm" | "reject" | "snooze",
    parentId?: string,
    editedContent?: string,
  ) {
    return this.request("POST", `/plugin-api/candidates/${encodeURIComponent(candidateId)}/decide`, {
      binding_id: bindingId,
      decision,
      parent_id: parentId,
      edited_content: editedContent,
    });
  }

  search(bindingId: string, q: string, limit = 5) {
    return this.request<{ results: unknown[] }>("POST", "/plugin-api/search", {
      binding_id: bindingId,
      q,
      limit,
    });
  }

  getNode(bindingId: string, nodeId: string) {
    return this.request(
      "GET",
      `/plugin-api/nodes/${encodeURIComponent(nodeId)}?binding_id=${encodeURIComponent(bindingId)}`,
    );
  }

  query(bindingId: string, question: string) {
    return this.request<{ answer: string; citations: unknown[] }>("POST", "/plugin-api/query", {
      binding_id: bindingId,
      question,
    });
  }

  status(bindingId: string) {
    return this.request<Record<string, unknown>>(
      "GET",
      `/plugin-api/status?binding_id=${encodeURIComponent(bindingId)}`,
    );
  }

  /** 结束记录: the session is complete; the core may merge it immediately. */
  closeDictation(bindingId: string, dictationId: string) {
    return this.request<{ ok: boolean; processing?: boolean; locked?: boolean }>(
      "POST",
      "/plugin-api/dictation/close",
      { binding_id: bindingId, dictation_id: dictationId },
      10_000,
    );
  }

  runDaily(bindingId: string) {
    return this.request<{
      locked?: boolean;
      candidates: number;
      pending_total: number;
      review_batch_size: number;
      notify: boolean;
      failed?: boolean;
    }>("POST", "/plugin-api/daily/run", { binding_id: bindingId }, 30 * 60 * 1000);
  }
}
