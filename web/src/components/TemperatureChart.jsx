export function TemperatureChart({ plot }) {
  if (!plot || plot.empty) {
    return <p className="chart-empty">No readings received yet</p>;
  }
  const plotLeft = plot.band?.x ?? 44;
  const plotRight = plot.threshold?.x2 ?? plot.width - 16;
  return (
    <figure>
      <svg
        className="temp-chart"
        viewBox={`0 0 ${plot.width} ${plot.height}`}
        role="img"
        aria-label={plot.label}
      >
        <text className="axis-unit" x="4" y="14">{plot.yLabel}</text>
        {plot.band ? (
          <rect className="band" x={plot.band.x} y={plot.band.y} width={plot.band.width} height={plot.band.height} />
        ) : null}
        {plot.threshold ? (
          <line className="threshold" x1={plot.threshold.x1} x2={plot.threshold.x2} y1={plot.threshold.y} y2={plot.threshold.y} />
        ) : null}
        {plot.markers.map((marker) => (
          <line key={marker.label} className={marker.label === "Contained" ? "marker contained" : "marker"} x1={marker.x} x2={marker.x} y1="18" y2={plot.height - 32}>
            <title>{marker.label}</title>
          </line>
        ))}
        {plot.ticks.map((tick) => (
          <text key={tick.label} className="tick" x={plotLeft - 8} y={tick.y + 3} textAnchor="end">{tick.label}</text>
        ))}
        <polyline className="trace" points={plot.line} />
        {plot.points.map((point) => (
          <circle key={`${point.x}-${point.y}-${point.title}`} className={point.hot ? "hot" : ""} cx={point.x} cy={point.y} r="4">
            <title>{point.title}</title>
          </circle>
        ))}
        {plot.xLabels.map((label) => (
          <text key={`${label.x}-${label.label}`} className="tick" x={label.x} y={plot.height - 14} textAnchor="middle">{label.label}</text>
        ))}
        {plot.zone ? (
          <text className="axis-unit" x={plotRight} y={plot.height - 2} textAnchor="end">{plot.zone}</text>
        ) : null}
      </svg>
      {plot.threshold ? (
        <figcaption className="chart-note">Dashed line: upper limit. Red points: above the limit.</figcaption>
      ) : null}
    </figure>
  );
}
