import type { Artifact } from '../api/types';
import {
  artifactsOf,
  cx,
  formatBytes,
  formatDateTime,
  isDownloadableArtifact,
} from '../lib/format';
import { Icon } from './Icon';
import { EmptyState, Spinner } from './ui';

export function Artifacts({
  artifacts,
  downloadingId,
  error,
  onDownload,
}: {
  artifacts: Artifact[];
  downloadingId: string | number | null;
  error: string | null;
  onDownload: (artifact: Artifact) => void;
}) {
  if (artifacts.length === 0) {
    return (
      <EmptyState
        compact
        icon="file"
        title="No artifacts yet"
        description="Reports, datasets and generated files land here when the workflow produces them."
      />
    );
  }

  return (
    <div className="artifact-list">
      {error ? <p className="inline-error">{error}</p> : null}
      {artifacts.map((artifact, index) => {
        const { name, kind } = artifactsOf(artifact);
        const downloadable = isDownloadableArtifact(artifact);
        const busy = downloadingId !== null && String(downloadingId) === String(artifact.id);
        return (
          <div className="artifact-row" key={artifact.id ?? `${name}-${index}`}>
            <span className="artifact-icon" aria-hidden="true">
              <Icon name={kind === 'markdown' || kind === 'report' ? 'file' : 'database'} size={16} />
            </span>
            <div className="artifact-info">
              <span className="artifact-name" title={name}>
                {name}
              </span>
              <span className="artifact-meta">
                <span className={cx('tag', 'tag--tiny', `tag--${kind === 'report' ? 'accent' : 'mono'}`)}>
                  {kind || 'file'}
                </span>
                {artifact.path ? (
                  <span className="artifact-path" title={artifact.path}>
                    {artifact.path}
                  </span>
                ) : null}
                {artifact.size_bytes ? <span>{formatBytes(artifact.size_bytes)}</span> : null}
                {artifact.created_at ? <span>{formatDateTime(artifact.created_at)}</span> : null}
              </span>
            </div>
            {downloadable ? (
              <button
                type="button"
                className="icon-btn"
                onClick={() => onDownload(artifact)}
                disabled={busy}
                aria-label={`Download ${name}`}
                title={`Download ${name}`}
              >
                {busy ? <Spinner size={14} /> : <Icon name="download" size={15} />}
              </button>
            ) : (
              <span className="artifact-no-download" title="Server-side artifact (not exposed for download)">
                server-side
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
