export function ConfigGate({ message }) {
  return (
    <main className="gate">
      <section className="gate-card">
        <h1>Desk unavailable</h1>
        <p role="alert">{message}</p>
      </section>
    </main>
  );
}
