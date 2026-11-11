/**
 * The subscription limits of the usage page: one meter for each window.
 *
 * A meter shows the percent used, a bar, the percent left, and the reset
 * time. `GET /api/v1/usage` derives every one of those, so this component
 * computes no percent and no time: the level of the bar is the class the
 * stylesheet colours (`ok`, `warn`, `crit`), and the reset time is the text
 * core's time gave in the instance timezone.
 *
 * A window that core marks `reset` keeps its last bar, dimmed by the `reset`
 * class, carries no badge, and says that the next model call updates it.
 *
 * The bar is inline SVG with attributes only, so the content policy of the
 * viewer blocks nothing.
 */

import type { ReactNode } from "react";

/** One window of the limits, as `GET /api/v1/usage` sends it. */
export interface LimitWindow {
  name: string;
  pct_used: string;
  pct_left: string;
  fill: number;
  level: string;
  badge: string;
  reset_at: string | null;
  reset_text: string;
  reset: boolean;
}

/** The limits, as `GET /api/v1/usage` sends them. */
export interface LimitsData {
  windows: LimitWindow[];
  as_of: string | null;
  reason: string | null;
}

/** What the section says with no `CORE_URL`, and when core said nothing. */
export const NOT_CONFIGURED_TEXT = "The limits are not configured. They need ";
export const NOT_AVAILABLE_TEXT = "The limits are not available now.";

/** The tone of the badge of a meter: a critical window is bad, a high one warns. */
function tone(level: string): string {
  return level === "crit" ? "bad" : "warn";
}

function Bar({ limit }: { limit: LimitWindow }) {
  return (
    <div
      className="limit-bar"
      role="meter"
      aria-label={`${limit.name} used`}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={limit.fill}
      aria-valuetext={`${limit.pct_used} used`}
    >
      <svg viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true">
        <rect className="track" width="100" height="8" rx="2" />
        <rect className="fill" width={limit.fill} height="8" rx="2" />
      </svg>
    </div>
  );
}

function Meter({ limit }: { limit: LimitWindow }) {
  const css = limit.reset ? `j-card limit ${limit.level} reset` : `j-card limit ${limit.level}`;
  return (
    <div className={css}>
      <div className="limit-head">
        <span className="limit-name">{limit.name}</span>
        {limit.badge !== "" && (
          <span className={`j-badge ${tone(limit.level)}`}>{limit.badge}</span>
        )}
        <span className="limit-value">
          <strong>{limit.pct_used}</strong> used
        </span>
      </div>
      <Bar limit={limit} />
      <div className="limit-foot">
        <span>{limit.pct_left} left</span>
        <span>
          {limit.reset_at === null ? (
            limit.reset_text
          ) : (
            <time dateTime={limit.reset_at}>{limit.reset_text}</time>
          )}
        </span>
      </div>
    </div>
  );
}

/**
 * The limits section. `limits` is null when core is not configured, did not
 * answer, or answered a shape the viewer does not know; `configured` says
 * which of those it is.
 */
export default function Meters({
  limits,
  configured,
}: {
  limits: LimitsData | null;
  configured: boolean;
}) {
  let body: ReactNode;
  if (!configured) {
    body = (
      <p className="j-empty">
        {NOT_CONFIGURED_TEXT}
        <code>CORE_URL</code>.
      </p>
    );
  } else if (limits === null || (limits.windows.length === 0 && limits.reason === null)) {
    body = <p className="j-empty">{NOT_AVAILABLE_TEXT}</p>;
  } else if (limits.windows.length === 0) {
    body = <p className="j-empty">{`The limits are not available: ${limits.reason}.`}</p>;
  } else {
    body = (
      <>
        {limits.as_of !== null && <p className="j-meta">{`as of ${limits.as_of}`}</p>}
        <div className="limits-grid">
          {limits.windows.map((limit) => (
            <Meter key={limit.name} limit={limit} />
          ))}
        </div>
      </>
    );
  }
  return (
    <section className="limits">
      <h2>Subscription limits</h2>
      {body}
    </section>
  );
}
