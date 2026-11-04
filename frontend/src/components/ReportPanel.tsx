import { Markdown } from './Markdown';
import { EmptyState, SkeletonLines } from './ui';
import { CopyButton } from './ui';
import { Icon } from './Icon';

export function ReportPanel({
  report,
  loading,
  terminal,
  failed,
}: {
  report: string | null;
  loading: boolean;
  terminal: boolean;
  failed: boolean;
}) {
  return (
    <section className="card panel report-panel" aria-labelledby="report-title">
      <header className="panel-head">
        <div className="panel-heading">
          <span className="panel-icon">
            <Icon name="file" size={16} />
          </span>
          <div>
            <h2 className="panel-title" id="report-title">
              Final report
            </h2>
            <p className="panel-subtitle">Synthesized business deliverable</p>
          </div>
        </div>
        {report ? (
          <div className="panel-actions">
            <CopyButton value={report} label="Copy markdown" />
          </div>
        ) : null}
      </header>
      <div className="panel-body report-body">
        {report ? (
          <Markdown source={report} />
        ) : loading ? (
          <div className="report-loading" aria-label="Loading report">
            <SkeletonLines lines={2} />
            <SkeletonLines lines={4} />
            <SkeletonLines lines={3} />
          </div>
        ) : terminal ? (
          <EmptyState
            compact
            icon={failed ? 'alert' : 'file'}
            title={failed ? 'Report unavailable' : 'No report generated'}
            description={
              failed
                ? 'The run failed before the synthesizer could produce a deliverable. Check the activity log for the failure point.'
                : 'This run finished without a synthesized report.'
            }
          />
        ) : (
          <EmptyState
            compact
            icon="sparkles"
            title="Report in progress"
            description="The synthesizer composes the final Markdown deliverable after every task has been evaluated. It will appear here automatically."
          />
        )}
      </div>
    </section>
  );
}
