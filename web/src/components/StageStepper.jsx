export function StageStepper({ progress }) {
  if (!progress) {
    return null;
  }
  return (
    <div className="stepper-wrap">
      <ol className="stepper" aria-label="Incident stage">
        {progress.stages.map((stage) => (
          <li
            key={stage.id}
            className={stage.status}
            aria-current={stage.status === "current" ? "step" : undefined}
          >
            <span className="step-mark" aria-hidden="true" />
            <span>{stage.label}</span>
          </li>
        ))}
      </ol>
      {progress.banner ? <p className="step-banner">{progress.banner}</p> : null}
    </div>
  );
}
