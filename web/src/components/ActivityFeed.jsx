import { useEffect, useRef } from "react";

export function ActivityFeed({ items }) {
  const ref = useRef(null);
  const signature = items.map((item) => item.id).join("\n");
  useEffect(() => {
    const node = ref.current;
    if (node) {
      node.scrollTop = node.scrollHeight;
    }
  }, [signature]);
  if (!items.length) {
    return <p className="chart-empty">No activity yet</p>;
  }
  return (
    <ol className="feed-list" ref={ref} aria-label="Activity">
      {items.map((item) => (
        <li key={item.id} className={`feed-item ${item.kind}`}>
          <time dateTime={item.at || undefined} title={item.title || undefined}>{item.time}</time>
          <p>
            {item.text}
            {item.tag ? <span className="badge advisory">{item.tag}</span> : null}
          </p>
        </li>
      ))}
    </ol>
  );
}
