import { useMemo, useState } from 'react';
import { api } from '../api/client';
import { useAsync } from '../hooks/useApi';
import { cx, truncate } from '../lib/format';
import { Icon } from '../components/Icon';
import { EmptyState, ErrorState, SkeletonLines } from '../components/ui';

export function ToolsPage() {
  const tools = useAsync(() => api.listTools(), []);
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState<string>('all');
  const list = useMemo(() => tools.data?.tools ?? [], [tools.data]);

  const categories = useMemo(() => {
    const values = new Set<string>();
    for (const tool of list) {
      if (tool.category) values.add(tool.category);
    }
    return Array.from(values).sort();
  }, [list]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return list.filter((tool) => {
      if (category !== 'all' && tool.category !== category) return false;
      if (!needle) return true;
      return (
        tool.name.toLowerCase().includes(needle) ||
        (tool.description ?? '').toLowerCase().includes(needle) ||
        (tool.category ?? '').toLowerCase().includes(needle)
      );
    });
  }, [list, query, category]);

  const sensitiveCount = list.filter((tool) => tool.sensitive).length;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <p className="kicker">Execution surface</p>
          <h1 className="page-title">Tool catalog</h1>
          <p className="page-lede">
            Every agent action is a registered tool call. Tools declare their input schema and
            scopes; sensitive tools only execute after an approved human gate.
          </p>
        </div>
        <span className="count-pill">
          <Icon name="tools" size={13} />
          {tools.loading && !tools.data ? 'Loading…' : `${list.length} tools · ${sensitiveCount} sensitive`}
        </span>
      </header>

      <div className="tool-toolbar">
        <div className="search-field">
          <Icon name="search" size={15} className="search-icon" />
          <label className="sr-only" htmlFor="tool-search">
            Search tools
          </label>
          <input
            id="tool-search"
            className="input input--search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search tools by name, category or description…"
            autoComplete="off"
          />
          {query ? (
            <button
              type="button"
              className="icon-btn input-suffix"
              onClick={() => setQuery('')}
              aria-label="Clear search"
            >
              <Icon name="x" size={13} />
            </button>
          ) : null}
        </div>
        <div className="filter-row" role="group" aria-label="Filter by category">
          <button
            type="button"
            className={cx('filter-chip', category === 'all' && 'is-active')}
            onClick={() => setCategory('all')}
            aria-pressed={category === 'all'}
          >
            All
            <span className="filter-count">{list.length}</span>
          </button>
          {categories.map((value) => (
            <button
              key={value}
              type="button"
              className={cx('filter-chip', category === value && 'is-active')}
              onClick={() => setCategory(value)}
              aria-pressed={category === value}
            >
              {value}
              <span className="filter-count">
                {list.filter((tool) => tool.category === value).length}
              </span>
            </button>
          ))}
        </div>
      </div>

      {tools.error ? (
        <ErrorState message={tools.error} onRetry={tools.reload} title="Could not load tools" />
      ) : tools.loading && !tools.data ? (
        <div className="tool-grid" aria-label="Loading tools">
          {Array.from({ length: 4 }).map((_, index) => (
            <div className="card tool-card" key={index}>
              <SkeletonLines lines={2} />
              <div style={{ height: 12 }} />
              <SkeletonLines lines={2} />
            </div>
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <EmptyState
          icon="tools"
          title={list.length === 0 ? 'No tools registered' : 'No tools match this filter'}
          description={
            list.length === 0
              ? 'The tool registry is empty. Agents will not be able to execute any actions.'
              : 'Try a different search term or category.'
          }
          action={
            query || category !== 'all' ? (
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => {
                  setQuery('');
                  setCategory('all');
                }}
              >
                Clear filters
              </button>
            ) : undefined
          }
        />
      ) : (
        <div className="tool-grid">
          {filtered.map((tool) => {
            const scopes = Array.isArray(tool.scopes) ? tool.scopes : [];
            return (
              <article className="card tool-card" key={tool.name}>
                <header className="tool-card-head">
                  <div className="tool-id">
                    <h2 className="tool-name mono">{tool.name}</h2>
                    {tool.category ? <span className="tag tag--tiny">{tool.category}</span> : null}
                  </div>
                  {tool.sensitive ? (
                    <span className="tag tag--warn">
                      <Icon name="lock" size={11} />
                      Sensitive
                    </span>
                  ) : null}
                </header>

                <p className="tool-desc">{tool.description ?? 'No description provided.'}</p>

                {scopes.length > 0 ? (
                  <div className="tool-section">
                    <span className="stat-label">Scopes</span>
                    <div className="tag-wrap">
                      {scopes.map((scope) => (
                        <span className="tag tag--mono tag--tiny" key={scope}>
                          {scope}
                        </span>
                      ))}
                    </div>
                  </div>
                ) : null}

                {tool.input_schema && Object.keys(tool.input_schema).length > 0 ? (
                  <details className="tool-schema">
                    <summary>
                      <Icon name="code" size={12} /> Input schema
                    </summary>
                    <pre>{JSON.stringify(tool.input_schema, null, 2)}</pre>
                  </details>
                ) : null}

                <footer className="tool-footer">
                  <span className={cx('tag', tool.sensitive ? 'tag--warn' : 'tag--muted')}>
                    <Icon name={tool.sensitive ? 'lock' : 'shield'} size={10} />
                    {tool.sensitive ? 'Approval gated' : 'Read-only'}
                  </span>
                  <span className="tool-desc-short" title={tool.description ?? ''}>
                    {truncate(tool.description ?? '', 42)}
                  </span>
                </footer>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
