import { useState, type FormEvent } from 'react';
import { api, errorMessage } from '../api/client';
import { setSession } from '../lib/session';
import { navigate, ROUTES } from '../lib/router';
import { Icon, LogoMark } from '../components/Icon';
import { Spinner } from '../components/ui';

export function LoginPage({ notice }: { notice: string | null }) {
  const [email, setEmail] = useState('demo@agentforge.io');
  const [password, setPassword] = useState('demo1234');
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (submitting) return;
    if (!email.trim() || !password) {
      setError('Enter your email and password to continue.');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const response = await api.login(email.trim(), password);
      setSession(response.access_token, response.user);
      navigate(ROUTES.dashboard, { replace: true });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-shell">
      <section className="login-visual" aria-label="About AgentForge">
        <div className="login-grid" aria-hidden="true" />
        <div className="login-visual-inner">
          <div className="brand brand--login">
            <LogoMark size={34} />
            <div className="brand-text">
              <span className="brand-name">AGENTFORGE</span>
              <span className="brand-sub">mission control</span>
            </div>
          </div>

          <div className="login-copy">
            <p className="kicker">Governed multi-agent execution</p>
            <h1>
              Autonomous operations,
              <br />
              under human command.
            </h1>
            <p className="login-lede">
              AgentForge turns a business objective into a planned, routed and evaluated
              multi-agent workflow — with approval gates for sensitive actions and a complete
              auditable trace.
            </p>
          </div>

          <ul className="login-features">
            <li>
              <span className="login-feature-icon">
                <Icon name="layers" size={16} />
              </span>
              <span>
                <strong>Plan → route → execute → evaluate</strong>
                A LangGraph orchestrator retries failing tasks with evaluator feedback.
              </span>
            </li>
            <li>
              <span className="login-feature-icon">
                <Icon name="shield" size={16} />
              </span>
              <span>
                <strong>Human-in-the-loop by default</strong>
                Sensitive tool calls pause the run until an operator approves.
              </span>
            </li>
            <li>
              <span className="login-feature-icon">
                <Icon name="activity" size={16} />
              </span>
              <span>
                <strong>Live trace + token accounting</strong>
                SSE event stream, per-task scores, artifacts and usage in one view.
              </span>
            </li>
          </ul>

          <div className="login-orbit" aria-hidden="true">
            <svg viewBox="0 0 320 200" fill="none">
              <defs>
                <linearGradient id="login-ring" x1="40" y1="20" x2="280" y2="190">
                  <stop stopColor="#3ee6c4" stopOpacity="0.55" />
                  <stop offset="1" stopColor="#5b8cff" stopOpacity="0.08" />
                </linearGradient>
              </defs>
              <ellipse cx="160" cy="100" rx="140" ry="52" stroke="url(#login-ring)" />
              <ellipse cx="160" cy="100" rx="96" ry="36" stroke="url(#login-ring)" strokeOpacity="0.6" />
              <ellipse cx="160" cy="100" rx="48" ry="18" stroke="url(#login-ring)" strokeOpacity="0.5" />
              <circle cx="160" cy="100" r="5" fill="#3ee6c4" />
              <circle cx="52" cy="80" r="3" fill="#5b8cff" />
              <circle cx="266" cy="124" r="3" fill="#a78bfa" />
              <circle cx="160" cy="52" r="2.5" fill="#3ee6c4" fillOpacity="0.8" />
            </svg>
            <span className="orbit-label orbit-label--one">planner</span>
            <span className="orbit-label orbit-label--two">executor</span>
            <span className="orbit-label orbit-label--three">evaluator</span>
          </div>

          <p className="login-version">v1.0 · demo workspace · sales analytics corpus loaded</p>
        </div>
      </section>

      <section className="login-panel" aria-label="Sign in">
        <div className="login-card card">
          <header className="login-card-head">
            <p className="kicker">Secure access</p>
            <h2>Sign in to mission control</h2>
            <p className="login-sub">Demo credentials are pre-filled — just enter.</p>
          </header>

          {notice ? (
            <div className="notice-banner" role="status">
              <Icon name="info" size={15} />
              <span>{notice}</span>
            </div>
          ) : null}

          <form className="login-form" onSubmit={(event) => void submit(event)} noValidate>
            <div className="field">
              <label className="field-label" htmlFor="login-email">
                Email
              </label>
              <input
                id="login-email"
                className="input"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                required
                disabled={submitting}
                placeholder="operator@agentforge.io"
                spellCheck={false}
              />
            </div>

            <div className="field">
              <label className="field-label" htmlFor="login-password">
                Password
              </label>
              <div className="input-group">
                <input
                  id="login-password"
                  className="input input--with-suffix"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                  minLength={6}
                  disabled={submitting}
                />
                <button
                  type="button"
                  className="icon-btn input-suffix"
                  onClick={() => setShowPassword((value) => !value)}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  aria-pressed={showPassword}
                >
                  <Icon name={showPassword ? 'eyeOff' : 'eye'} size={15} />
                </button>
              </div>
            </div>

            {error ? (
              <p className="login-error" role="alert">
                <Icon name="alert" size={14} />
                {error}
              </p>
            ) : null}

            <button
              type="submit"
              className="btn btn-primary btn-lg btn-block"
              disabled={submitting || !email.trim() || !password}
            >
              {submitting ? (
                <>
                  <Spinner size={15} />
                  Authenticating…
                </>
              ) : (
                <>
                  Enter mission control
                  <Icon name="arrowRight" size={16} />
                </>
              )}
            </button>
          </form>

          <div className="demo-note">
            <Icon name="shield" size={14} />
            <span>
              Demo account <code>demo@agentforge.io</code> · <code>demo1234</code>
            </span>
          </div>
        </div>

        <p className="login-footer">
          AgentForge · governed multi-agent execution ·{' '}
          <a href="/docs" target="_blank" rel="noopener noreferrer">
            API docs
          </a>
        </p>
      </section>
    </div>
  );
}
