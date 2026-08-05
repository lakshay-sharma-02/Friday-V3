/**
 * Friday Companion — API client for the local Friday V4 mobile API.
 *
 * The phone talks to ``friday4 mobile serve`` on the desktop (pure-stdlib
 * HTTP server, default port 8900). Every endpoint the companion needs is
 * typed here so a contract change fails the app's typecheck.
 *
 * Endpoints (see src/friday_v4/mobile/api.py):
 *   GET  /api/status            transport health + shared-thread summary
 *   GET  /api/conversation      today's shared session (one presence)
 *   POST /api/talk              one NLU point — the same brain as talk/voice/web
 *   GET  /api/events            SSE stream over the durable ambient queue
 *   GET  /api/devices           paired phones (tokens never leaked)
 *   POST /api/devices/register  pair this phone (one-time code + push token)
 *   DELETE /api/devices/:id     unpair
 */
import { Platform } from 'react-native';

export interface StatusPayload {
  available: boolean;
  shared_session: {
    id: string;
    surface?: string | null;
    started_at?: string | null;
  } | null;
  exchanges_today: number;
  error?: string;
}

export interface Exchange {
  role: string;
  content: string;
  intent?: string;
  created_at?: string;
}

export interface ConversationPayload {
  available: boolean;
  session_id: string | null;
  exchanges: Exchange[];
}

export interface TalkResult {
  text?: string;
  intent?: string;
  action?: string;
  response: string;
  action_type?: string;
  command?: string | null;
  goal?: string | null;
  mission_id?: string | null;
  action_id?: string | null;
  status?: string;
}

export interface DeviceInfo {
  id: string;
  platform?: string;
  name?: string;
  created_at?: string;
  last_seen?: string | null;
}

export interface DevicesPayload {
  devices: DeviceInfo[];
}

export interface AmbientEvent {
  id: number;
  topic?: string | null;
  payload?: unknown;
  priority?: string | null;
  source?: string | null;
  created_at?: string | null;
}

export interface RegisterResult {
  ok: boolean;
  device_id?: string;
  error?: string;
}

/**
 * The desktop is reachable at a different address per device kind:
 * Android emulator maps the host's loopback to 10.0.2.2; the iOS
 * simulator shares localhost; a physical phone needs the desktop's LAN
 * IP (set it on the Status screen — it is persisted).
 */
export function defaultBaseUrl(): string {
  return Platform.OS === 'android'
    ? 'http://10.0.2.2:8900'
    : 'http://localhost:8900';
}

export class ApiClient {
  constructor(
    public readonly baseUrl: string,
    public readonly token?: string | null,
  ) {}

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      ...(init?.headers as Record<string, string> | undefined),
    };
    // Optional bearer token — set on the Status screen. The API is
    // open on the LAN by default; once Friday is exposed over a public
    // tunnel, the same secret gates the power.
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    const res = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers,
    });
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`Friday API ${res.status}: ${body || res.statusText}`);
    }
    return (await res.json()) as T;
  }

  status(): Promise<StatusPayload> {
    return this.request<StatusPayload>('/api/status');
  }

  conversation(limit = 40): Promise<ConversationPayload> {
    return this.request<ConversationPayload>(
      `/api/conversation?limit=${limit}`,
    );
  }

  talk(text: string): Promise<TalkResult> {
    return this.request<TalkResult>('/api/talk', {
      method: 'POST',
      body: JSON.stringify({ text }),
    });
  }

  devices(): Promise<DevicesPayload> {
    return this.request<DevicesPayload>('/api/devices');
  }

  /** Pairing is the one endpoint that legitimately answers 401 — parse it. */
  async registerDevice(
    code: string,
    token: string,
    name: string,
  ): Promise<RegisterResult> {
    let res: Response;
    try {
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
      };
      if (this.token) headers.Authorization = `Bearer ${this.token}`;
      res = await fetch(`${this.baseUrl}/api/devices/register`, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          code: code.trim().toUpperCase(),
          token,
          name: name.trim(),
          platform: Platform.OS,
        }),
      });
    } catch (err) {
      return {
        ok: false,
        error: err instanceof Error ? err.message : 'network error',
      };
    }
    const data = (await res.json().catch(() => ({}))) as RegisterResult;
    // Spread first so the status-derived ok is authoritative.
    return { ...data, ok: res.status === 200 && !!data.ok };
  }

  removeDevice(id: string): Promise<{ ok: boolean; removed?: boolean }> {
    return this.request<{ ok: boolean; removed?: boolean }>(
      `/api/devices/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
    );
  }
}
