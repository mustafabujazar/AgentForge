/**
 * Incremental Server-Sent Events parser.
 *
 * The dashboard cannot use the browser `EventSource` because the API requires an
 * `Authorization: Bearer` header, which EventSource cannot send. We therefore
 * read `fetch()`'s ReadableStream and feed decoded text through this parser.
 */

export interface SseFrame {
  event: string;
  data: string;
  id?: string;
  retry?: number;
}

export interface SseParser {
  push(chunk: string): void;
  flush(): void;
}

interface Boundary {
  index: number;
  length: number;
}

function findBoundary(buffer: string): Boundary | null {
  const candidates: Boundary[] = [];
  const lf = buffer.indexOf('\n\n');
  if (lf !== -1) candidates.push({ index: lf, length: 2 });
  const crlf = buffer.indexOf('\r\n\r\n');
  if (crlf !== -1) candidates.push({ index: crlf, length: 4 });
  const cr = buffer.indexOf('\r\r');
  if (cr !== -1) candidates.push({ index: cr, length: 2 });
  if (!candidates.length) return null;
  return candidates.reduce((best, current) => (current.index < best.index ? current : best));
}

function dispatch(raw: string, onFrame: (frame: SseFrame) => void): void {
  const lines = raw.split(/\r\n|\n|\r/);
  const dataLines: string[] = [];
  let event = 'message';
  let id: string | undefined;
  let retry: number | undefined;

  for (const line of lines) {
    if (line === '' || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);

    switch (field) {
      case 'event':
        event = value || 'message';
        break;
      case 'data':
        dataLines.push(value);
        break;
      case 'id':
        id = value;
        break;
      case 'retry': {
        const parsed = Number(value);
        if (Number.isFinite(parsed)) retry = parsed;
        break;
      }
      default:
        break;
    }
  }

  if (dataLines.length === 0) return;
  const frame: SseFrame = { event, data: dataLines.join('\n') };
  if (id !== undefined) frame.id = id;
  if (retry !== undefined) frame.retry = retry;
  onFrame(frame);
}

export function createSseParser(onFrame: (frame: SseFrame) => void): SseParser {
  let buffer = '';

  return {
    push(chunk: string): void {
      buffer += chunk;
      let boundary = findBoundary(buffer);
      while (boundary) {
        const raw = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary.length);
        if (raw.trim().length > 0) dispatch(raw, onFrame);
        boundary = findBoundary(buffer);
      }
    },
    flush(): void {
      if (buffer.trim().length > 0) {
        dispatch(buffer, onFrame);
        buffer = '';
      }
    },
  };
}
