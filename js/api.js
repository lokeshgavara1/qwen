// All communication with the AI Gateway backend (qwen_lb.py) lives here.
// The gateway supports an optional API key for usage tracking, but this
// frontend only ever talks to it in public (no-key) mode.

import { GATEWAY_URL, ENDPOINTS } from './config.js';
import { safeJsonParse } from './utils.js';

class GatewayError extends Error {
  constructor(message, status = null) {
    super(message);
    this.name = 'GatewayError';
    this.status = status;
  }
}

async function request(path, { method = 'GET', body, signal, timeoutMs = 15000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const combinedSignal = signal || controller.signal;

  try {
    const res = await fetch(`${GATEWAY_URL}${path}`, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
      signal: combinedSignal,
    });

    if (!res.ok) {
      const errData = safeJsonParse(await res.text().catch(() => ''));
      throw new GatewayError(errData?.error || `Gateway responded with ${res.status}`, res.status);
    }
    return res.json();
  } catch (err) {
    if (err.name === 'AbortError') {
      throw new GatewayError('Request timed out. Is the gateway running?');
    }
    if (err instanceof GatewayError) throw err;
    throw new GatewayError(err.message || 'Network error contacting the gateway.');
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Send a prompt to /api/generate. Supports both streaming (ndjson, default)
 * and single-shot JSON responses. When streaming, `onToken(chunk, fullText)`
 * fires as each token arrives.
 */
export async function generateResponse(prompt, {
  stream = true,
  images = [],
  model = null,
  onToken = null,
  signal = null,
} = {}) {
  const payload = { prompt, images, stream };
  if (model) payload.model = model; // omit for "auto" so the gateway's intent detection picks it

  let res;
  try {
    res = await fetch(`${GATEWAY_URL}${ENDPOINTS.GENERATE}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    });
  } catch (err) {
    if (err.name === 'AbortError') {
      throw new GatewayError('Request timed out. Is the gateway running?');
    }
    throw new GatewayError(`Network error connecting to gateway at ${GATEWAY_URL}: ${err.message}`);
  }

  if (!res.ok) {
    const errData = safeJsonParse(await res.text().catch(() => ''));
    throw new GatewayError(errData?.error || `Gateway responded with ${res.status}`, res.status);
  }

  const server = res.headers.get('X-Server');
  const servedModel = res.headers.get('X-Model');

  if (!stream || !res.body) {
    const data = await res.json();
    return {
      text: data.response ?? '',
      raw: data,
      server: data._server || server,
      model: data._model || servedModel,
      cached: !!data._cached,
      evalCount: data.eval_count ?? null,
      totalDurationNs: data.total_duration ?? null,
    };
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let fullText = '';
  let final = null;

  const consumeLine = (line) => {
    if (!line.trim()) return;
    const obj = safeJsonParse(line);
    if (!obj) return;
    if (obj.response) {
      fullText += obj.response;
      if (onToken) onToken(obj.response, fullText);
    }
    if (obj.done) final = obj;
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();
    lines.forEach(consumeLine);
  }
  if (buffer.trim()) consumeLine(buffer);

  return {
    text: fullText,
    raw: final,
    server,
    model: servedModel,
    cached: false,
    evalCount: final?.eval_count ?? null,
    totalDurationNs: final?.total_duration ?? null,
  };
}

export function getHealth() {
  return request(ENDPOINTS.HEALTH);
}

export { GatewayError };
