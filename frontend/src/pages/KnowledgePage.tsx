import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { useAsync } from '../hooks/useApi';
import { formatPercent, truncate } from '../lib/format';
import { Icon } from '../components/Icon';
import { EmptyState, ErrorState, SkeletonLines, Spinner } from '../components/ui';

const SUGGESTIONS = [
  'pricing policy',
  'quality process',
  'marketing playbook',
  'support escalation',
  'product notes',
];

function scorePercent(score: number | null | undefined): number | null {
  if (score === null || score === undefined || !Number.isFinite(score)) return null;
  const percent = score <= 1 ? score * 100 : score;
  return Math.max(0, Math.min(100, percent));
}

export function KnowledgePage() {
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query.trim()), 350);
    return () => window.clearTimeout(timer);
  }, [query]);

  const search = useAsync(
    () => api.searchKnowledge(debounced, 8),
    [debounced],
    debounced.length > 0,
  );
  const results = search.data?.results ?? [];
  const searching = search.loading;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <p className="kicker">Retrieval corpus</p>
          <h1 className="page-title">Knowledge base</h1>
          <p className="page-lede">
            Internal memos are chunked, embedded and retrieved into agent context. Search the live
            RAG store to see exactly what the agents can ground their reasoning in.
          </p>
        </div>
        <span className="count-pill">
          <Icon name="knowledge" size={13} />
          vector + keyword hybrid
        </span>
      </header>

      <div className="card knowledge-search-card">
        <div className="search-field search-field--lg">
          {searching ? (
            <Spinner size={16} className="search-icon" />
          ) : (
            <Icon name="search" size={17} className="search-icon" />
          )}
          <label className="sr-only" htmlFor="knowledge-search">
            Search knowledge base
          </label>
          <input
            id="knowledge-search"
            className="input input--search input--lg"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Ask the corpus — e.g. “pricing policy for enterprise accounts”"
            autoComplete="off"
            autoFocus
          />
          {query ? (
            <button
              type="button"
              className="icon-btn input-suffix"
              onClick={() => setQuery('')}
              aria-label="Clear search"
            >
              <Icon name="x" size={14} />
            </button>
          ) : null}
        </div>

        <div className="knowledge-suggestions" role="group" aria-label="Suggested searches">
          <span className="presets-label">
            <Icon name="sparkles" size={12} />
            Try
          </span>
          {SUGGESTIONS.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              className="preset-pill"
              onClick={() => setQuery(suggestion)}
            >
              {suggestion}
            </button>
          ))}
        </div>
      </div>

      {!debounced ? (
        <div className="knowledge-intro card">
          <span className="knowledge-intro-icon">
            <Icon name="database" size={24} />
          </span>
          <div>
            <h2>Search the retrieval corpus</h2>
            <p>
              Results show the chunk text, its source document and the similarity score used when
              assembling agent context. Type a query or pick a suggestion above.
            </p>
          </div>
        </div>
      ) : search.error ? (
        <ErrorState message={search.error} onRetry={search.reload} title="Search failed" />
      ) : searching ? (
        <div className="knowledge-results" aria-label="Searching knowledge base">
          {Array.from({ length: 3 }).map((_, index) => (
            <div className="card knowledge-card" key={index}>
              <SkeletonLines lines={2} />
              <div style={{ height: 12 }} />
              <SkeletonLines lines={3} />
            </div>
          ))}
        </div>
      ) : results.length === 0 ? (
        <EmptyState
          icon="search"
          title="No matching chunks"
          description={
            <>
              Nothing in the knowledge store matched <code>“{debounced}”</code>. Try a broader
              term, or ingest documents via <code>POST /api/knowledge/ingest</code>.
            </>
          }
        />
      ) : (
        <>
          <div className="knowledge-result-head">
            <span>
              <strong>{results.length}</strong> chunk{results.length === 1 ? '' : 's'} retrieved
            </span>
            <span className="muted-text">
              for <code>“{debounced}”</code>
            </span>
          </div>
          <div className="knowledge-results">
            {results.map((result, index) => {
              const score = scorePercent(result.score);
              return (
                <article className="card knowledge-card" key={`${result.chunk_id ?? result.title ?? 'chunk'}-${index}`}>
                  <header className="knowledge-card-head">
                    <div>
                      <h2 className="knowledge-title">
                        {result.title ?? `Chunk ${result.chunk_id ?? index + 1}`}
                      </h2>
                      <p className="knowledge-source">
                        <Icon name="file" size={12} />
                        {result.source ?? 'unknown source'}
                      </p>
                    </div>
                    {score !== null ? (
                      <span
                        className={
                          score >= 60
                            ? 'score-pill score-pill--high'
                            : score >= 35
                              ? 'score-pill'
                              : 'score-pill score-pill--low'
                        }
                      >
                        {formatPercent(score / 100, 1)} match
                      </span>
                    ) : null}
                  </header>
                  <p className="knowledge-text">{truncate(result.text ?? '—', 420)}</p>
                  {score !== null ? (
                    <div className="score-bar" aria-hidden="true">
                      <span style={{ width: `${score}%` }} />
                    </div>
                  ) : null}
                </article>
              );
            })}
          </div>
        </>
      )}

    </div>
  );
}
