/**
 * Compact, dependency-free Markdown parser + React renderer support.
 *
 * Supported: h1–h6 (h4+ are rendered as h3-level styling), bold, italic, inline
 * code, fenced code blocks, ordered/unordered lists (one nested level), tables,
 * links, blockquotes, horizontal rules and paragraphs. Raw HTML is never
 * injected, so rendering untrusted model output is safe by construction.
 */

export type InlineNode =
  | { type: 'text'; text: string }
  | { type: 'strong'; children: InlineNode[] }
  | { type: 'em'; children: InlineNode[] }
  | { type: 'code'; text: string }
  | { type: 'link'; href: string; children: InlineNode[] }
  | { type: 'br' };

export interface MarkdownListItem {
  children: InlineNode[];
  sub: MarkdownList | null;
}

export interface MarkdownList {
  ordered: boolean;
  start: number;
  items: MarkdownListItem[];
}

export type MarkdownBlock =
  | { type: 'heading'; level: number; children: InlineNode[] }
  | { type: 'paragraph'; children: InlineNode[] }
  | { type: 'code'; lang: string; text: string }
  | { type: 'list'; list: MarkdownList }
  | { type: 'table'; header: InlineNode[][]; rows: InlineNode[][][]; align: Array<'left' | 'center' | 'right' | null> }
  | { type: 'quote'; blocks: MarkdownBlock[] }
  | { type: 'hr' };

const HEADING_RE = /^(#{1,6})\s+(.+?)\s*#*\s*$/;
const FENCE_RE = /^(?:`{3,}|~{3,})\s*([A-Za-z0-9_+.+-]*)\s*$/;
const FENCE_CLOSE_RE = /^(?:`{3,}|~{3,})\s*$/;
const HR_RE = /^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$/;
const QUOTE_RE = /^\s{0,3}>\s?(.*)$/;
const UL_RE = /^(\s*)[-*+]\s+(.*)$/;
const OL_RE = /^(\s*)\d{1,9}[.)]\s+(.*)$/;
const TABLE_SEPARATOR_RE = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$/;

function isWordChar(char: string | undefined): boolean {
  return !!char && /[A-Za-z0-9]/.test(char);
}

function indentOf(line: string): number {
  const match = /^\s*/.exec(line);
  return match ? match[0].length : 0;
}

export function parseInline(text: string): InlineNode[] {
  const nodes: InlineNode[] = [];
  let buffer = '';
  let index = 0;

  const flush = (): void => {
    if (buffer) {
      nodes.push({ type: 'text', text: buffer });
      buffer = '';
    }
  };

  while (index < text.length) {
    const char = text[index];

    // Backslash escapes the next character.
    if (char === '\\' && index + 1 < text.length) {
      buffer += text[index + 1];
      index += 2;
      continue;
    }

    // Inline code spans.
    if (char === '`') {
      const end = text.indexOf('`', index + 1);
      if (end > index + 1) {
        flush();
        nodes.push({ type: 'code', text: text.slice(index + 1, end) });
        index = end + 1;
        continue;
      }
    }

    // Bold / italic.
    if (char === '*' || char === '_') {
      const doubled = text[index + 1] === char;
      const marker = doubled ? char + char : char;
      const wordBoundaryOk = doubled || !isWordChar(text[index - 1]);
      if (wordBoundaryOk) {
        const close = text.indexOf(marker, index + marker.length);
        if (close > index + marker.length) {
          const inner = text.slice(index + marker.length, close);
          if (inner.trim().length > 0) {
            flush();
            nodes.push({
              type: doubled ? 'strong' : 'em',
              children: parseInline(inner),
            });
            index = close + marker.length;
            continue;
          }
        }
      }
    }

    // Links [label](href "optional title")
    if (char === '[') {
      const match = /^\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/.exec(text.slice(index));
      if (match) {
        flush();
        nodes.push({ type: 'link', href: match[2], children: parseInline(match[1]) });
        index += match[0].length;
        continue;
      }
    }

    // Autolinks <https://…>
    if (char === '<') {
      const match = /^<((?:https?:\/\/|mailto:)[^>\s]+)>/.exec(text.slice(index));
      if (match) {
        flush();
        nodes.push({ type: 'link', href: match[1], children: [{ type: 'text', text: match[1] }] });
        index += match[0].length;
        continue;
      }
    }

    buffer += char;
    index += 1;
  }

  flush();
  return nodes;
}

interface ListMarker {
  ordered: boolean;
  indent: number;
  start: number;
  text: string;
}

function matchListMarker(line: string): ListMarker | null {
  const unordered = UL_RE.exec(line);
  if (unordered) {
    return { ordered: false, indent: unordered[1].length, start: 1, text: unordered[2] };
  }
  const ordered = OL_RE.exec(line);
  if (ordered) {
    const digits = /^\s*(\d+)/.exec(line);
    return {
      ordered: true,
      indent: ordered[1].length,
      start: digits ? Number(digits[1]) : 1,
      text: ordered[2],
    };
  }
  return null;
}

function parseList(lines: string[], startIndex: number): { list: MarkdownList; next: number } {
  const first = matchListMarker(lines[startIndex]);
  if (!first) return { list: { ordered: false, start: 1, items: [] }, next: startIndex + 1 };

  const ordered = first.ordered;
  const baseIndent = first.indent;
  const items: MarkdownListItem[] = [];
  let index = startIndex;

  while (index < lines.length) {
    const line = lines[index];
    const marker = matchListMarker(line);

    if (!marker) break;
    if (marker.ordered !== ordered || marker.indent !== baseIndent) break;

    const parts: string[] = [marker.text];
    let sub: MarkdownList | null = null;
    index += 1;

    while (index < lines.length) {
      const current = lines[index];
      if (current.trim() === '') {
        // A blank line continues the item only when the next line is indented.
        const nextIndent = index + 1 < lines.length ? indentOf(lines[index + 1]) : 0;
        if (nextIndent > baseIndent) {
          parts.push('');
          index += 1;
          continue;
        }
        break;
      }

      const nested = matchListMarker(current);
      if (nested && nested.indent > baseIndent) {
        if (!sub) {
          const parsed = parseList(lines, index);
          sub = parsed.list;
          index = parsed.next;
        } else {
          index += 1;
        }
        continue;
      }

      if (nested && nested.indent === baseIndent) break;

      if (indentOf(current) > baseIndent) {
        parts.push(current.trim());
        index += 1;
        continue;
      }

      break;
    }

    const content = parts.join(' ').replace(/\s+/g, ' ').trim();
    items.push({ children: parseInline(content), sub });
  }

  return { list: { ordered, start: first.start, items }, next: index };
}

function splitRow(line: string): string[] {
  let value = line.trim();
  if (value.startsWith('|')) value = value.slice(1);
  if (value.endsWith('|')) value = value.slice(0, -1);

  const cells: string[] = [];
  let current = '';
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if (char === '\\' && index + 1 < value.length) {
      current += value[index + 1];
      index += 1;
      continue;
    }
    if (char === '|') {
      cells.push(current.trim());
      current = '';
      continue;
    }
    current += char;
  }
  cells.push(current.trim());
  return cells;
}

function parseAlignments(separator: string, columns: number): Array<'left' | 'center' | 'right' | null> {
  const cells = splitRow(separator);
  const align = cells.map((cell) => {
    const left = cell.startsWith(':');
    const right = cell.endsWith(':');
    if (left && right) return 'center' as const;
    if (right) return 'right' as const;
    if (left) return 'left' as const;
    return null;
  });
  while (align.length < columns) align.push(null);
  return align.slice(0, columns);
}

function isBlockStart(lines: string[], index: number): boolean {
  const line = lines[index];
  if (FENCE_RE.test(line)) return true;
  if (HEADING_RE.test(line)) return true;
  if (HR_RE.test(line)) return true;
  if (QUOTE_RE.test(line)) return true;
  if (matchListMarker(line)) return true;
  if (line.includes('|') && index + 1 < lines.length && TABLE_SEPARATOR_RE.test(lines[index + 1])) {
    return true;
  }
  return false;
}

function parseBlocks(lines: string[]): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (line.trim() === '') {
      index += 1;
      continue;
    }

    // Fenced code block.
    const fence = FENCE_RE.exec(line);
    if (fence) {
      const lang = fence[1] ?? '';
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !FENCE_CLOSE_RE.test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push({ type: 'code', lang, text: code.join('\n') });
      continue;
    }

    // Heading.
    const heading = HEADING_RE.exec(line);
    if (heading) {
      const level = Math.min(6, Math.max(1, heading[1].length));
      blocks.push({ type: 'heading', level, children: parseInline(heading[2].trim()) });
      index += 1;
      continue;
    }

    // Horizontal rule.
    if (HR_RE.test(line)) {
      blocks.push({ type: 'hr' });
      index += 1;
      continue;
    }

    // Table.
    if (line.includes('|') && index + 1 < lines.length && TABLE_SEPARATOR_RE.test(lines[index + 1])) {
      const headerCells = splitRow(line);
      const align = parseAlignments(lines[index + 1], headerCells.length);
      const rows: InlineNode[][][] = [];
      index += 2;
      while (index < lines.length && lines[index].trim() !== '' && lines[index].includes('|')) {
        const cells = splitRow(lines[index]).slice(0, headerCells.length);
        while (cells.length < headerCells.length) cells.push('');
        rows.push(cells.map((cell) => parseInline(cell)));
        index += 1;
      }
      blocks.push({
        type: 'table',
        header: headerCells.map((cell) => parseInline(cell)),
        rows,
        align,
      });
      continue;
    }

    // Blockquote.
    const quote = QUOTE_RE.exec(line);
    if (quote) {
      const quoteLines: string[] = [];
      while (index < lines.length) {
        const current = QUOTE_RE.exec(lines[index]);
        if (!current) break;
        quoteLines.push(current[1]);
        index += 1;
      }
      blocks.push({ type: 'quote', blocks: parseBlocks(quoteLines) });
      continue;
    }

    // Lists.
    if (matchListMarker(line)) {
      const parsed = parseList(lines, index);
      if (parsed.list.items.length > 0) {
        blocks.push({ type: 'list', list: parsed.list });
        index = parsed.next;
        continue;
      }
    }

    // Paragraph: consume until blank line or another block starts.
    const paragraph: string[] = [];
    while (index < lines.length) {
      const current = lines[index];
      if (current.trim() === '') break;
      if (paragraph.length > 0 && isBlockStart(lines, index)) break;
      paragraph.push(current.trim());
      index += 1;
    }
    if (paragraph.length > 0) {
      blocks.push({ type: 'paragraph', children: parseInline(paragraph.join(' ')) });
    } else {
      index += 1;
    }
  }

  return blocks;
}

export function parseMarkdown(source: string): MarkdownBlock[] {
  if (!source || !source.trim()) return [];
  const normalized = source
    .replace(/^\uFEFF/, '')
    .replace(/\r\n?/g, '\n');
  return parseBlocks(normalized.split('\n'));
}
