/**
 * Error boundary.
 *
 * Without one, a throw anywhere in the tree makes React discard the *whole* root — so a bug in one
 * map layer presents as a completely blank console with no clue as to what failed. For an operator
 * tool that is the worst possible failure mode: the screen a person is relying on goes dark and
 * says nothing.
 *
 * Wrapping panels individually means a broken map still leaves the event feed, the alerts inbox and
 * the copilot usable, and the failure names itself.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** Shown in the fallback, so the message says which panel failed. */
  label: string;
}

interface State {
  error: Error | null;
  stack: string | null;
}

export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null, stack: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // Keep it in the console too: the fallback is for the operator, this is for whoever debugs it.
    console.error(`[${this.props.label}] render failed`, error, info.componentStack);
    this.setState({ stack: info.componentStack ?? null });
  }

  private reset = (): void => {
    this.setState({ error: null, stack: null });
  };

  override render(): ReactNode {
    const { error, stack } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="panel-error" role="alert">
        <h3>{this.props.label} failed to render</h3>
        <p className="panel-error-message">{error.message}</p>
        <div className="panel-error-actions">
          <button onClick={this.reset}>Retry</button>
          <button onClick={() => window.location.reload()}>Reload console</button>
        </div>
        {stack && (
          <details>
            <summary>component stack</summary>
            <pre>{stack.trim()}</pre>
          </details>
        )}
      </div>
    );
  }
}
