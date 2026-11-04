import { useMemo, type ReactNode } from 'react';
import {
  parseMarkdown,
  type InlineNode,
  type MarkdownBlock,
  type MarkdownList,
} from '../lib/markdown';
import { cx, safeHref } from '../lib/format';
import { CopyButton } from './ui';

function renderInline(nodes: InlineNode[]): ReactNode {
  return nodes.map((node, index) => {
    switch (node.type) {
      case 'text':
        return <span key={index}>{node.text}</span>;
      case 'strong':
        return <strong key={index}>{renderInline(node.children)}</strong>;
      case 'em':
        return <em key={index}>{renderInline(node.children)}</em>;
      case 'code':
        return (
          <code key={index} className="md-inline-code">
            {node.text}
          </code>
        );
      case 'link': {
        const href = safeHref(node.href);
        if (!href) return <span key={index}>{renderInline(node.children)}</span>;
        return (
          <a key={index} href={href} target="_blank" rel="noopener noreferrer">
            {renderInline(node.children)}
          </a>
        );
      }
      case 'br':
        return <br key={index} />;
      default:
        return null;
    }
  });
}

function renderList(list: MarkdownList, keyPrefix: string): ReactNode {
  const items = list.items.map((item, index) => (
    <li key={`${keyPrefix}-${index}`}>
      {renderInline(item.children)}
      {item.sub ? renderList(item.sub, `${keyPrefix}-${index}-nested`) : null}
    </li>
  ));
  return list.ordered ? (
    <ol key={keyPrefix} start={list.start === 1 ? undefined : list.start}>
      {items}
    </ol>
  ) : (
    <ul key={keyPrefix}>{items}</ul>
  );
}

function renderBlock(block: MarkdownBlock, index: number): ReactNode {
  switch (block.type) {
    case 'heading': {
      const level = Math.min(3, Math.max(1, block.level));
      const Tag = (`h${level}`) as 'h1' | 'h2' | 'h3';
      return (
        <Tag key={index} className={`md-h md-h${block.level}`}>
          {renderInline(block.children)}
        </Tag>
      );
    }
    case 'paragraph':
      return <p key={index}>{renderInline(block.children)}</p>;
    case 'code':
      return (
        <div className="md-code" key={index}>
          <div className="md-code-head">
            <span className="md-code-lang">{block.lang || 'text'}</span>
            <CopyButton value={block.text} compact />
          </div>
          <pre>
            <code>{block.text}</code>
          </pre>
        </div>
      );
    case 'list':
      return <div key={index}>{renderList(block.list, `list-${index}`)}</div>;
    case 'table':
      return (
        <div className="md-table-wrap" key={index}>
          <table>
            <thead>
              <tr>
                {block.header.map((cell, cellIndex) => (
                  <th key={cellIndex} style={{ textAlign: block.align[cellIndex] ?? undefined }}>
                    {renderInline(cell)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex} style={{ textAlign: block.align[cellIndex] ?? undefined }}>
                      {renderInline(cell)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case 'quote':
      return (
        <blockquote key={index}>
          {block.blocks.map((inner, innerIndex) => renderBlock(inner, innerIndex))}
        </blockquote>
      );
    case 'hr':
      return <hr key={index} />;
    default:
      return null;
  }
}

export function Markdown({ source, className }: { source: string; className?: string }) {
  const blocks = useMemo(() => parseMarkdown(source), [source]);
  if (blocks.length === 0) return null;
  return <div className={cx('markdown', className)}>{blocks.map(renderBlock)}</div>;
}
