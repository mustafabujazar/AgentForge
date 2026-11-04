import type { TokenUsage as TokenUsageType } from '../api/types';
import { cx, formatNumber } from '../lib/format';

export function TokenUsage({ usage }: { usage: TokenUsageType | null | undefined }) {
  const prompt = usage?.prompt_tokens ?? 0;
  const completion = usage?.completion_tokens ?? 0;
  const total = usage?.total_tokens ?? prompt + completion;
  const calls = usage?.calls ?? 0;
  const denominator = prompt + completion || 1;
  const promptShare = Math.max(0, Math.min(100, (prompt / denominator) * 100));
  const completionShare = Math.max(0, Math.min(100, (completion / denominator) * 100));
  const hasUsage = total > 0 || calls > 0;

  return (
    <div className={cx('token-usage', !hasUsage && 'token-usage--empty')}>
      <div className="token-total">
        <span className="token-total-value">{formatNumber(total)}</span>
        <span className="token-total-label">total tokens</span>
      </div>

      <div className="token-bar" aria-hidden="true">
        <span className="token-bar-prompt" style={{ width: `${promptShare}%` }} />
        <span className="token-bar-completion" style={{ width: `${completionShare}%` }} />
      </div>

      <dl className="token-rows">
        <div className="token-row">
          <dt>
            <span className="token-dot token-dot--prompt" /> Prompt
          </dt>
          <dd>{formatNumber(prompt)}</dd>
        </div>
        <div className="token-row">
          <dt>
            <span className="token-dot token-dot--completion" /> Completion
          </dt>
          <dd>{formatNumber(completion)}</dd>
        </div>
        <div className="token-row">
          <dt>
            <span className="token-dot token-dot--calls" /> LLM calls
          </dt>
          <dd>{formatNumber(calls)}</dd>
        </div>
      </dl>

      {!hasUsage ? <p className="token-empty-hint">Usage appears after the first agent call.</p> : null}
    </div>
  );
}
