/**
 * The bar chart of the usage: one bar for each day of the window, today
 * included, stacked by group.
 *
 * The window is `days` days back from today in the instance timezone, so a
 * longer window shows a longer axis also when the usage stops short of it. A
 * day with no usage is an empty slot, and a window with no usage at all is an
 * empty chart that says so.
 *
 * The chart is inline SVG with attributes only, and the colour of each
 * segment is a series class of the stylesheet (`.s0` to `.s7`), so the page
 * loads nothing more and holds no colour of its own.
 */

import { dayTotal, exact, series, short, type UsageBody, type UsageDay } from "./data";

// The chart, in SVG user units.
const CHART_W = 640;
const CHART_H = 220;
const PAD_L = 48;
const PAD_R = 8;
const PAD_T = 12;
const PAD_B = 28;

const PLOT_W = CHART_W - PAD_L - PAD_R;
const PLOT_H = CHART_H - PAD_T - PAD_B;

// The longest window core answers, which holds the axis to a known length.
const MAX_DAYS = 366;

/** What an empty window says in the plot. */
export const EMPTY_TEXT = "no usage in this window";

/** A day `count` days after `day`, both `YYYY-MM-DD`. */
function addDays(day: string, count: number): string {
  const when = new Date(`${day}T00:00:00Z`);
  when.setUTCDate(when.getUTCDate() + count);
  return when.toISOString().slice(0, 10);
}

/** Every day of the window, oldest first, `today` last. */
function windowDays(body: UsageBody, today: string): string[] {
  const span = Math.max(1, Math.min(body.days, MAX_DAYS));
  const first = addDays(today, 1 - span);
  return Array.from({ length: span }, (_item, index) => addDays(first, index));
}

export default function Chart({ body, today }: { body: UsageBody; today: string }) {
  const days = windowDays(body, today);
  const byDay = new Map(body.rows.map((row): [string, UsageDay] => [row.date, row]));
  const totals = days.map((day) => {
    const row = byDay.get(day);
    return row === undefined ? 0 : dayTotal(row);
  });
  const peak = Math.max(...totals) || 1;
  const slot = PLOT_W / days.length;
  const bar = Math.max(slot * 0.8, 1.0);
  const ticks = [0, Math.floor(peak / 2), peak];
  const last = days[days.length - 1] ?? today;
  return (
    <figure className="j-card usage-chart">
      <svg
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        role="img"
        aria-label="Total tokens per day, stacked by group"
      >
        {ticks.map((tick, place) => {
          const y = PAD_T + PLOT_H - (PLOT_H * tick) / peak;
          return (
            <g key={place}>
              <line
                className="grid"
                x1={PAD_L}
                x2={CHART_W - PAD_R}
                y1={y.toFixed(1)}
                y2={y.toFixed(1)}
              />
              <text className="axis" x={PAD_L - 6} y={(y + 4).toFixed(1)} textAnchor="end">
                {short(tick)}
              </text>
            </g>
          );
        })}
        {days.map((day, index) => {
          const row = byDay.get(day);
          if (row === undefined) {
            return null;
          }
          const x = PAD_L + index * slot + (slot - bar) / 2;
          let base = PAD_T + PLOT_H;
          return (
            <g key={day}>
              {body.groups.map((name, place) => {
                const group = row.groups[name];
                if (group === undefined || group.total <= 0) {
                  return null;
                }
                const height = (PLOT_H * group.total) / peak;
                base -= height;
                return (
                  <rect
                    key={name}
                    className={series(place)}
                    x={x.toFixed(1)}
                    y={base.toFixed(1)}
                    width={bar.toFixed(1)}
                    height={height.toFixed(1)}
                  >
                    <title>{`${day} · ${name}: ${exact(group.total)} tokens`}</title>
                  </rect>
                );
              })}
            </g>
          );
        })}
        {!totals.some((total) => total > 0) && (
          <text
            className="axis"
            x={(CHART_W / 2).toFixed(0)}
            y={(PAD_T + PLOT_H / 2).toFixed(0)}
            textAnchor="middle"
          >
            {EMPTY_TEXT}
          </text>
        )}
        <text className="axis" x={PAD_L} y={CHART_H - 8}>
          {days[0]}
        </text>
        <text className="axis" x={CHART_W - PAD_R} y={CHART_H - 8} textAnchor="end">
          {last}
        </text>
      </svg>
      <ul className="legend">
        {body.groups.map((name, place) => (
          <li key={name}>
            <span className={`sw ${series(place)}`}></span>
            {name}
          </li>
        ))}
      </ul>
    </figure>
  );
}
