/**
 * Friday Companion — SSE hook over the durable ambient queue.
 *
 * ``GET /api/events?since=<id>`` streams every ambient event the daemon /
 * security / suggestions publish, replaying from a cursor so a
 * reconnecting phone misses nothing. We stream it with fetch's readable
 * body (RN supports streaming responses) and parse ``id:``/``data:``
 * frames by hand — no external SSE dependency.
 */
import { useEffect, useRef, useState } from 'react';
import type { AmbientEvent } from './api';

export interface SseState {
  connected: boolean;
  events: AmbientEvent[];
}

export function useEvents(
  baseUrl: string | null,
  maxEvents = 120,
  token?: string | null,
): SseState {
  const [state, setState] = useState<SseState>({
    connected: false,
    events: [],
  });
  const maxRef = useRef(maxEvents);
  maxRef.current = maxEvents;

  useEffect(() => {
    if (!baseUrl) {
      setState({ connected: false, events: [] });
      return;
    }
    let cancelled = false;
    let controller: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let live = false;

    const connect = async () => {
      controller = new AbortController();
      try {
        // Unlike the PWA's EventSource, fetch can set headers — send
        // the token as Authorization so it never rides in a URL.
        const headers: Record<string, string> = {
          Accept: 'text/event-stream',
        };
        if (token) headers.Authorization = `Bearer ${token}`;
        const res = await fetch(`${baseUrl}/api/events?since=0`, {
          headers,
          signal: controller.signal,
        });
        if (!res.ok || !res.body) {
          throw new Error(`events HTTP ${res.status}`);
        }
        live = true;
        setState((s) => ({ ...s, connected: true }));
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        // Read forever until the server closes or we abort.
        // eslint-disable-next-line no-constant-condition
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let sep: number;
          while ((sep = buffer.indexOf('\n\n')) >= 0) {
            const frame = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            let id = 0;
            let data = '';
            for (const line of frame.split('\n')) {
              if (line.startsWith('id:')) {
                id = Number(line.slice(3).trim()) || 0;
              } else if (line.startsWith('data:')) {
                data += line.slice(5).trim();
              }
            }
            if (data) {
              try {
                const ev = JSON.parse(data) as AmbientEvent;
                if (!cancelled) {
                  setState((s) => ({
                    connected: true,
                    events: [...s.events, ev].slice(-maxRef.current),
                  }));
                }
              } catch {
                // malformed frame — keep the stream alive
              }
            }
          }
        }
      } catch {
        // server closed / network dropped — reconnect below
      } finally {
        if (!cancelled && live) {
          setState((s) => ({ ...s, connected: false }));
        }
      }
      if (!cancelled) {
        timer = setTimeout(connect, 3000);
      }
    };

    connect();
    return () => {
      cancelled = true;
      if (timer) {
        clearTimeout(timer);
      }
      controller?.abort();
    };
  }, [baseUrl, token]);

  return state;
}
