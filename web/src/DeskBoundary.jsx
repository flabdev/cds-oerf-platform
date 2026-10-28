import { Component, cloneElement, isValidElement } from "react";

export class DeskBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { failed: false, attempt: 0 };
    this.retry = this.retry.bind(this);
  }

  static getDerivedStateFromError() {
    return { failed: true };
  }

  retry() {
    this.setState((state) => ({ failed: false, attempt: state.attempt + 1 }));
  }

  render() {
    if (this.state.failed) {
      return (
        <main className="gate">
          <section className="gate-card">
            <h1>Desk unavailable</h1>
            <p role="alert">The desk could not draw this view.</p>
            <button type="button" className="send" onClick={this.retry}>Retry</button>
          </section>
        </main>
      );
    }
    const child = this.props.children;
    if (isValidElement(child)) {
      return cloneElement(child, { key: this.state.attempt });
    }
    return child;
  }
}
