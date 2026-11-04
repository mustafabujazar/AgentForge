import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from '../api/client';

export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

export interface AsyncResult<T> extends AsyncState<T> {
  reload: () => void;
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

/**
 * Runs an async loader whenever `deps` change. The loader is held in a ref so
 * inline arrow functions don't retrigger the effect on every render.
 */
export function useAsync<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  enabled = true,
): AsyncResult<T> {
  const [state, setState] = useState<AsyncState<T>>({
    data: null,
    error: null,
    loading: enabled,
  });
  const [nonce, setNonce] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    if (!enabled) {
      setState({ data: null, error: null, loading: false });
      return;
    }
    const controller = new AbortController();
    let active = true;
    setState((previous) => ({ data: previous.data, error: null, loading: true }));

    loaderRef.current(controller.signal).then(
      (data) => {
        if (active) setState({ data, error: null, loading: false });
      },
      (error: unknown) => {
        if (!active || isAbort(error)) return;
        setState((previous) => ({
          data: previous.data,
          error: errorMessage(error),
          loading: false,
        }));
      },
    );

    return () => {
      active = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled, nonce]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  return { ...state, reload };
}

/** Async loader with optional silent polling (stops between polls, not on errors). */
export function usePolledAsync<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  options: { enabled?: boolean; intervalMs?: number } = {},
): AsyncResult<T> {
  const { enabled = true, intervalMs = 0 } = options;
  const [state, setState] = useState<AsyncState<T>>({
    data: null,
    error: null,
    loading: enabled,
  });
  const [nonce, setNonce] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    if (!enabled) {
      setState({ data: null, error: null, loading: false });
      return;
    }
    let active = true;
    let controller = new AbortController();
    let timer: number | null = null;

    const run = async (initial: boolean) => {
      if (initial) setState((previous) => ({ ...previous, loading: true, error: null }));
      try {
        const data = await loaderRef.current(controller.signal);
        if (active) setState({ data, error: null, loading: false });
      } catch (error) {
        if (!active || isAbort(error)) return;
        setState((previous) => ({
          data: previous.data,
          error: errorMessage(error),
          loading: false,
        }));
      }
    };

    void run(true);
    if (intervalMs > 0) {
      timer = window.setInterval(() => {
        controller.abort();
        controller = new AbortController();
        void run(false);
      }, intervalMs);
    }

    return () => {
      active = false;
      controller.abort();
      if (timer !== null) window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled, intervalMs, nonce]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  return { ...state, reload };
}

/** Ticking timestamp for live relative-clock displays. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}
