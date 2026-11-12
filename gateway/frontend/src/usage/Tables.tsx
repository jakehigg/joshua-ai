/**
 * The tables of the usage page, in the order the page shows them:
 *
 * - the days by group, days down and groups across, with a total row;
 * - the totals of each group, with the models of each one;
 * - the workers of each group that has workers;
 * - the counts of each day, one fold-out for each day.
 *
 * Every group of `groups` is in the tables of totals, also a group with no
 * usage, and a fold-out of a day holds only the groups with usage that day. A
 * count is exact, with a thin space between each group of three digits.
 */

import { Fragment } from "react";

import {
  COLUMNS,
  allGroups,
  dayTotal,
  exact,
  type CountKey,
  type UsageBody,
  type UsageCounts,
} from "./data";

/** What a row with no model says. */
export const NO_MODELS = "—";

/** The models of a row, joined, or a dash when the row named none. */
export function modelsText(models: string[]): string {
  return models.length > 0 ? models.join(", ") : NO_MODELS;
}

/** One count of each row added. */
function sum(rows: UsageCounts[], key: CountKey): number {
  return rows.reduce((total, row) => total + row[key], 0);
}

/**
 * One row for each group, the six counts across, and a total row. With
 * `models`, a last column holds the models of each row.
 */
function CountsTable({
  groups,
  label = "Group",
  models = false,
}: {
  groups: Record<string, UsageCounts>;
  label?: string;
  models?: boolean;
}) {
  const rows = Object.entries(groups);
  const counts = rows.map(([, row]) => row);
  return (
    <div className="j-table-wrap">
      <table className="j-table usage">
        <thead>
          <tr>
            <th>{label}</th>
            {COLUMNS.map(([key, name]) => (
              <th className="num" key={key}>
                {name}
              </th>
            ))}
            {models && <th>Models</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, row]) => (
            <tr key={name}>
              <th>{name}</th>
              {COLUMNS.map(([key]) => (
                <td className="num" key={key}>
                  {exact(row[key])}
                </td>
              ))}
              {models && <td>{modelsText(row.models)}</td>}
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th>Total</th>
            {COLUMNS.map(([key]) => (
              <td className="num" key={key}>
                <strong>{exact(sum(counts, key))}</strong>
              </td>
            ))}
            {models && <td></td>}
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

/** Days down, groups across, the total tokens in each cell. */
function DaysTable({ body }: { body: UsageBody }) {
  const grand = Object.values(body.totals).reduce((total, group) => total + group.total, 0);
  return (
    <>
      <h2>Tokens by day</h2>
      <div className="j-table-wrap">
        <table className="j-table usage">
          <thead>
            <tr>
              <th>Date</th>
              {body.groups.map((name) => (
                <th className="num" key={name}>
                  {name}
                </th>
              ))}
              <th className="num">Total</th>
            </tr>
          </thead>
          <tbody>
            {body.rows.map((row) => (
              <tr key={row.date}>
                <th>{row.date}</th>
                {body.groups.map((name) => {
                  const group = row.groups[name];
                  return (
                    <td className="num" key={name}>
                      {group === undefined ? "" : exact(group.total)}
                    </td>
                  );
                })}
                <td className="num">
                  <strong>{exact(dayTotal(row))}</strong>
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th>Total</th>
              {body.groups.map((name) => {
                const group = body.totals[name];
                return group === undefined ? (
                  <td key={name}></td>
                ) : (
                  <td className="num" key={name}>
                    <strong>{exact(group.total)}</strong>
                  </td>
                );
              })}
              <td className="num">
                <strong>{exact(grand)}</strong>
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </>
  );
}

/** The totals of each group with its models, then the workers of each group. */
function Totals({ body }: { body: UsageBody }) {
  return (
    <>
      <h2>Totals by group</h2>
      <CountsTable groups={allGroups(body)} models />
      {body.groups.map((name) => {
        const group = body.totals[name];
        if (group === undefined || Object.keys(group.workers).length === 0) {
          return null;
        }
        return (
          <Fragment key={name}>
            <h3>{`The workers of ${name}`}</h3>
            <CountsTable groups={group.workers} label="Worker" models />
          </Fragment>
        );
      })}
    </>
  );
}

/** One fold-out for each day, with the groups that used tokens that day. */
function EachDay({ body }: { body: UsageBody }) {
  return (
    <>
      <h2>Each day</h2>
      {body.rows.map((row) => (
        <details className="day" key={row.date}>
          <summary>{row.date}</summary>
          <CountsTable groups={row.groups} />
        </details>
      ))}
    </>
  );
}

export default function Tables({ body }: { body: UsageBody }) {
  return (
    <>
      {body.rows.length > 0 && <DaysTable body={body} />}
      <Totals body={body} />
      {body.rows.length > 0 && <EachDay body={body} />}
    </>
  );
}
