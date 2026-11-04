import { useState } from 'react';
import type { ApprovalDecision, PendingApproval } from '../api/types';
import { Icon } from './Icon';
import { Spinner, StatusBadge } from './ui';

export function ApprovalBanner({
  approval,
  deciding,
  error,
  onDecision,
}: {
  approval: PendingApproval;
  deciding: ApprovalDecision | null;
  error: string | null;
  onDecision: (decision: ApprovalDecision, note: string) => void;
}) {
  const [note, setNote] = useState('');
  const busy = deciding !== null;

  return (
    <section className="approval-banner" role="region" aria-label="Human approval required">
      <div className="approval-accent" aria-hidden="true" />
      <div className="approval-icon" aria-hidden="true">
        <Icon name="lock" size={22} />
      </div>

      <div className="approval-main">
        <div className="approval-kicker">
          <span className="approval-kicker-text">Human approval required</span>
          <StatusBadge status="awaiting_approval" size="sm" />
        </div>
        <h2 className="approval-title">{approval.title ?? 'Sensitive action requested'}</h2>
        <p className="approval-action">
          {approval.requested_action ??
            'The workflow is paused before executing a sensitive operation.'}
        </p>
        {approval.reason ? (
          <p className="approval-reason">
            <span className="approval-reason-label">Reason</span>
            {approval.reason}
          </p>
        ) : null}
        {approval.task_id ? (
          <p className="approval-meta">
            Task <code>{approval.task_id}</code>
            {approval.id ? (
              <>
                {' · '}Approval <code>{approval.id}</code>
              </>
            ) : null}
          </p>
        ) : null}

        <div className="approval-note">
          <label className="field-label" htmlFor="approval-note">
            Decision note <span className="field-hint">optional — recorded in the audit trail</span>
          </label>
          <textarea
            id="approval-note"
            className="input input--area"
            rows={2}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="e.g. Verified against the Q3 ledger; proceed with the adjustment."
            disabled={busy}
          />
        </div>

        {error ? (
          <p className="inline-error" role="alert">
            <Icon name="alert" size={13} />
            {error}
          </p>
        ) : null}
      </div>

      <div className="approval-actions">
        <button
          type="button"
          className="btn btn-success"
          onClick={() => onDecision('approved', note)}
          disabled={busy}
        >
          {deciding === 'approved' ? <Spinner size={14} /> : <Icon name="check" size={15} />}
          Approve &amp; resume
        </button>
        <button
          type="button"
          className="btn btn-danger"
          onClick={() => onDecision('rejected', note)}
          disabled={busy}
        >
          {deciding === 'rejected' ? <Spinner size={14} /> : <Icon name="x" size={15} />}
          Reject
        </button>
        <p className="approval-hint">Sensitive tool execution is blocked until decided.</p>
      </div>
    </section>
  );
}
