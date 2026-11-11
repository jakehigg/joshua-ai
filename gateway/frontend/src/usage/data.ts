/**
 * The usage of the instance, as `GET /api/v1/usage` sends it.
 *
 * `usage` is core's own answer, unchanged: the tokens of each group by day.
 * The shapes here are the shapes of `joshua_shared.contracts`, so the chart
 * and the tables read the same names core writes.
 *
 * The two number formats are the formats of the page: an exact count with a
 * thin space between each group of three digits, and a short count for an
 * axis label.
 */

/** The counts of one group or one worker. */
export interface UsageCounts {
  calls: number;
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
  total: number;
  models: string[];
}

/** The counts of one group, and of each worker in it. */
export interface UsageGroup extends UsageCounts {
  workers: Record<string, UsageCounts>;
}

/** One calendar day, and the groups that used tokens on it. */
export interface UsageDay {
  date: string;
  groups: Record<string, UsageGroup>;
}

/** Core's answer: the tokens of each group by day, newest day first. */
export interface UsageBody {
  timezone: string;
  days: number;
  groups: string[];
  rows: UsageDay[];
  totals: Record<string, UsageGroup>;
}

/** The key of one count of a table. */
export type CountKey = "calls" | "input" | "output" | "cache_read" | "cache_write" | "total";

/** The six counts of a table, in the order of its columns. */
export const COLUMNS: readonly (readonly [CountKey, string])[] = [
  ["calls", "Calls"],
  ["input", "Input"],
  ["output", "Output"],
  ["cache_read", "Cache read"],
  ["cache_write", "Cache write"],
  ["total", "Total"],
];

/** A group or a worker with no usage at all. */
export const NO_COUNTS: UsageCounts = {
  calls: 0,
  input: 0,
  output: 0,
  cache_read: 0,
  cache_write: 0,
  total: 0,
  models: [],
};

/** The number of series colours in the stylesheet (`.s0` to `.s7`). */
export const SERIES_COLORS = 8;

/** The colour class of a group, by its place in `groups`. */
export function series(index: number): string {
  return `s${index % SERIES_COLORS}`;
}

/** An exact integer with a thin space between each group of three digits. */
export function exact(value: number): string {
  return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

/** A count for a chart label: `950`, `12k`, `3.4M`. */
export function short(value: number): string {
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1)}M`.replace(".0M", "M");
  }
  if (value >= 1_000) {
    return `${Math.round(value / 1_000)}k`;
  }
  return String(value);
}

/** The total tokens of one day, every group added. */
export function dayTotal(row: UsageDay): number {
  return Object.values(row.groups).reduce((sum, group) => sum + group.total, 0);
}

/** The counts of each group of `groups`, also a group with no usage. */
export function allGroups(body: UsageBody): Record<string, UsageCounts> {
  const groups: Record<string, UsageCounts> = {};
  for (const name of body.groups) {
    groups[name] = body.totals[name] ?? NO_COUNTS;
  }
  return groups;
}
