import { useEffect, useRef } from 'react';
import type { CSSProperties } from 'react';
import type { TaskView } from '../lib/runData';
import { agentColor, agentLabel } from '../lib/runData';
import {
  cx,
  evaluationFeedback,
  evaluationIssues,
  evaluationScore,
  resultText,
  statusMeta,
} from '../lib/format';
import { Icon } from './Icon';
import { AttemptDots, EmptyState, ScoreRing, StatusBadge } from './ui';

function slug(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]/g, '-');
}

export function TaskBoard({
  tasks,
  highlightedId,
  emptyHint,
}: {
  tasks: TaskView[];
  highlightedId?: string | null;
  emptyHint?: string;
}) {
  const cardsRef = useRef(new Map<string, HTMLElement>());

  useEffect(() => {
    if (!highlightedId) return;
    const node = cardsRef.current.get(highlightedId);
    if (node) node.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [highlightedId]);

  if (tasks.length === 0) {
    return (
      <EmptyState
        compact
        icon="layers"
        title="Plan not available yet"
        description={emptyHint ?? 'The planner has not published its task graph for this run.'}
      />
    );
  }

  return (
    <div className="task-grid">
      {tasks.map((task, index) => {
        const meta = statusMeta(task.status);
        const score = evaluationScore(task.evaluation);
        const issues = evaluationIssues(task.evaluation);
        const feedback = evaluationFeedback(task.evaluation);
        const result = resultText(task.result);
        const agentStyle = {
          '--agent-color': agentColor(task.agent),
        } as CSSProperties;

        return (
          <article
            key={task.id}
            id={`task-card-${slug(task.id)}`}
            ref={(node) => {
              if (node) cardsRef.current.set(task.id, node);
              else cardsRef.current.delete(task.id);
            }}
            className={cx(
              'task-card',
              `tone-${meta.tone}`,
              highlightedId === task.id && 'is-highlighted',
            )}
            aria-label={`Task ${index + 1}: ${task.title}`}
          >
            <header className="task-head">
              <div className="task-heading">
                <span className="task-order">{String(index + 1).padStart(2, '0')}</span>
                <h3 className="task-title">{task.title}</h3>
              </div>
              <StatusBadge status={task.status} size="sm" />
            </header>

            {task.description ? <p className="task-desc">{task.description}</p> : null}

            <div className="task-tags">
              {task.agent ? (
                <span className="agent-chip agent-chip--sm" style={agentStyle}>
                  {agentLabel(task.agent)}
                </span>
              ) : (
                <span className="tag tag--muted">Unassigned</span>
              )}
              {task.sensitive ? (
                <span className="tag tag--warn">
                  <Icon name="lock" size={11} />
                  Sensitive
                </span>
              ) : null}
              {task.tools.slice(0, 3).map((tool) => (
                <span className="tag tag--mono" key={tool} title={tool}>
                  {tool}
                </span>
              ))}
              {task.tools.length > 3 ? (
                <span className="tag tag--muted">+{task.tools.length - 3}</span>
              ) : null}
            </div>

            <div className="task-stats">
              <div className="task-score">
                <ScoreRing score={score} size={42} />
                <span className="stat-label">Eval</span>
              </div>
              <div className="task-stat">
                <AttemptDots attempts={task.attempts} />
              </div>
              {task.dependencies.length > 0 ? (
                <div className="task-stat task-deps">
                  <span className="stat-label">After</span>
                  <span className="task-dep-list" title={task.dependencies.join(', ')}>
                    {task.dependencies.map((dependency) => (
                      <span key={dependency} className="tag tag--mono tag--tiny">
                        {dependency}
                      </span>
                    ))}
                  </span>
                </div>
              ) : null}
            </div>

            {result ? (
              <details className="task-details">
                <summary>
                  <Icon name="terminal" size={12} /> Result
                </summary>
                <pre className="task-result">{result}</pre>
              </details>
            ) : null}

            {issues.length > 0 || feedback ? (
              <details className="task-details task-details--warn">
                <summary>
                  <Icon name="gauge" size={12} /> Evaluation
                  {score !== null ? ` · ${Math.round(score)}%` : ''}
                </summary>
                {issues.length > 0 ? (
                  <ul className="issue-list">
                    {issues.map((issue, issueIndex) => (
                      <li key={issueIndex}>{issue}</li>
                    ))}
                  </ul>
                ) : null}
                {feedback ? <p className="feedback-text">{feedback}</p> : null}
              </details>
            ) : null}

            {task.successCriteria.length > 0 ? (
              <details className="task-details task-details--muted">
                <summary>
                  <Icon name="check" size={12} /> Success criteria
                </summary>
                <ul className="criteria-list">
                  {task.successCriteria.map((criterion, criterionIndex) => (
                    <li key={criterionIndex}>{criterion}</li>
                  ))}
                </ul>
              </details>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}
