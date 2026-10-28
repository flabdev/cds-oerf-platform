import { stateMeta } from "../states.js";

export function StatePill({ state }) {
  const meta = stateMeta(state);
  return <span className={`pill tone-${meta.tone}`}>{meta.label}</span>;
}
