import { useEffect, useRef, useState } from 'react';
import { api, isApiError, openEventStream } from '../api/client';
import { createSseParser } from '../lib/sse';
import type { ConnectionState } from '../components/ui';
import type { RunEvent } from '../api/types';

export interface LiveEventsResult {
  events: RunEvent[];
  connection: ConnectionState;
  lastSeq: number;
}

/**
 * Live run activity.
 *
 * Primary transport is the SSE endpoint via fetch + ReadableStream (so the
 * bearer token can be sent). If the stream fails, is unsupported, or ends
 * early, we fall back to polling `GET /events?after_seq` every 1.5s and retry
 * SSE every ~9s. `seq` is deduplicated and monotonic even across reconnects.
 */
export function useLiveEvents(runId: string | null, terminal: boolean): LiveEventsResult {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [connection, setConnection] = useState<ConnectionState>('connecting');
  const [lastSeq, setLastSeq] = useState(0);

  const lastSeqRef = useRef(0);
  const terminalRef = useRef(terminal);
  const stopRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    terminalRef.current = terminal;
    if (terminal && stopRef.current) stopRef.current();
  }, [terminal]);

  useEffect(() => {
    if (!runId) {
      setEvents([]);
      setLastSeq(0);
      setConnection('connecting');
      return;
    }

    let disposed = false;
    let streamController: AbortController | null = null;
    let pollTimer: number | null = null;
    let retryTimer: number | null = null;
    let polling = false;
    let failures = 0;
    const seenSeq = new Set<number>();

    lastSeqRef.current = 0;
    setEvents([]);
    setLastSeq(0);
    setConnection('connecting');

    const addEvents = (incoming: RunEvent[]): void => {
      if (disposed) return;
      const fresh: RunEvent[] = [];
      for (const event of incoming) {
        if (!event || typeof event.seq !== 'number' || typeof event.type !== 'string') continue;
        if (event.seq <= lastSeqRef.current || seenSeq.has(event.seq)) continue;
        seenSeq.add(event.seq);
        fresh.push(event);
      }
      if (fresh.length === 0) return;
      fresh.sort((a, b) => a.seq - b.seq);
      lastSeqRef.current = fresh[fresh.length - 1].seq;
      setLastSeq(lastSeqRef.current);
      setEvents((previous) => [...previous, ...fresh]);
    };

    const stopStream = (): void => {
      if (streamController) {
        streamController.abort();
        streamController = null;
      }
    };

    const stopPolling = (): void => {
      if (pollTimer !== null) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
      polling = false;
    };

    const clearRetry = (): void => {
      if (retryTimer !== null) {
        window.clearTimeout(retryTimer);
        retryTimer = null;
      }
    };

    const pollOnce = async (): Promise<boolean> => {
      if (disposed) return false;
      try {
        const batch = await api.getEvents(runId, lastSeqRef.current);
        if (disposed) return false;
        addEvents(Array.isArray(batch) ? batch : []);
        failures = 0;
        if (!terminalRef.current) setConnection('polling');
        return true;
      } catch (error) {
        if (disposed) return false;
        if (isApiError(error) && error.status === 401) return false;
        failures += 1;
        if (failures >= 2) setConnection('offline');
        return false;
      }
    };

    const startPolling = (): void => {
      if (disposed || terminalRef.current || polling) return;
      polling = true;
      setConnection('polling');
      void pollOnce();
      pollTimer = window.setInterval(() => {
        if (disposed || terminalRef.current) {
          stopPolling();
          return;
        }
        void pollOnce();
      }, 1500);
    };

    const scheduleRetry = (): void => {
      if (disposed || terminalRef.current || retryTimer !== null) return;
      retryTimer = window.setTimeout(() => {
        retryTimer = null;
        if (!disposed && !terminalRef.current) void connectStream();
      }, 9000);
    };

    const connectStream = async (): Promise<void> => {
      if (disposed || terminalRef.current) return;
      stopStream();
      const controller = new AbortController();
      streamController = controller;

      try {
        const response = await openEventStream(runId, lastSeqRef.current, controller.signal);
        if (disposed || controller.signal.aborted) {
          controller.abort();
          return;
        }
        if (!response.body) throw new Error('Streaming response has no body');

        stopPolling();
        failures = 0;
        setConnection('live');

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const parser = createSseParser((frame) => {
          if (disposed) return;
          try {
            const parsed = JSON.parse(frame.data) as unknown;
            if (
              parsed &&
              typeof parsed === 'object' &&
              'seq' in parsed &&
              'type' in parsed
            ) {
              addEvents([parsed as RunEvent]);
            }
          } catch {
            /* non-JSON frame / heartbeat */
          }
        });

        let endedNaturally = false;
        while (!disposed && !terminalRef.current) {
          const { done, value } = await reader.read();
          if (done) {
            endedNaturally = true;
            break;
          }
          if (value) parser.push(decoder.decode(value, { stream: true }));
        }
        parser.flush();

        if (disposed) return;
        if (terminalRef.current) {
          setConnection('closed');
          return;
        }
        if (endedNaturally) {
          startPolling();
          scheduleRetry();
        }
      } catch (error) {
        if (disposed) return;
        if (error instanceof DOMException && error.name === 'AbortError') return;
        if (isApiError(error) && error.status === 401) return;
        failures += 1;
        startPolling();
        if (failures >= 2) setConnection('offline');
        scheduleRetry();
      }
    };

    const handleTerminal = (): void => {
      stopStream();
      stopPolling();
      clearRetry();
      setConnection('closed');
      // One final catch-up poll so trailing events are never missed.
      void pollOnce().then(() => {
        if (!disposed) setConnection('closed');
      });
    };
    stopRef.current = handleTerminal;

    if (terminalRef.current) {
      // Deep-linked to a finished run: backfill history once, never stream.
      setConnection('polling');
      void pollOnce().then(() => {
        if (!disposed) setConnection('closed');
      });
    } else {
      void connectStream();
    }

    return () => {
      disposed = true;
      stopRef.current = null;
      stopStream();
      stopPolling();
      clearRetry();
    };
  }, [runId]);

  return { events, connection, lastSeq };
}
