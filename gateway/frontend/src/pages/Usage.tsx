/**
 * `/usage?days=<n>`: the subscription limits, and the tokens of each group by
 * day.
 *
 * The day range is the query of the address, so a bookmark opens the same
 * window. `GET /api/v1/usage` makes both calls to core at the same time and
 * derives every number of a meter, so this page shows the limits, the chart
 * and the tables and computes no percent and no reset time.
 *
 * The page answers also when core is not configured or did not answer: the
 * limits section and the usage each say so on their own, so one of them is
 * there when the other is not.
 */

import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { ApiError, getJson } from "../api/client";
import Shell from "../shell/Shell";
import Chip from "../ui/Chip";
import Empty from "../ui/Empty";
import ErrorLine from "../ui/ErrorLine";
import Page from "../ui/Page";
import Spinner from "../ui/Spinner";
import Chart from "../usage/Chart";
import Meters, { type LimitsData } from "../usage/Meters";
import Tables from "../usage/Tables";
import type { UsageBody } from "../usage/data";

/** The answer of `GET /api/v1/usage`. */
export interface UsageAnswer {
  days: number;
  usage_configured: boolean;
  usage: UsageBody | null;
  limits_configured: boolean;
  limits: LimitsData | null;
  timezone: string;
  now: string;
}

/** The day ranges the page offers. */
export const WINDOWS = [7, 30, 90, 366];

/** What the page says when core did not answer, and when there is nothing. */
export const NO_CORE_TEXT = "Core did not answer. The usage is not available now.";
export const EMPTY_TEXT = "No usage in this window.";

/** The usage of the window, or the line that says why there is none. */
function Body({ answer }: { answer: UsageAnswer }) {
  if (!answer.usage_configured) {
    return (
      <p className="j-empty">
        The usage page is not configured. It needs <code>CORE_URL</code> and{" "}
        <code>JOSHUA_TOKEN_VIEWER</code>.
      </p>
    );
  }
  const usage = answer.usage;
  if (usage === null) {
    return <Empty text={NO_CORE_TEXT} />;
  }
  return (
    <>
      <p className="j-muted">
        {`Tokens of the last ${usage.days} days, by calendar day in ${usage.timezone}. ` +
          "A day with no usage is not in the tables of days."}
      </p>
      <Chart body={usage} today={answer.now.slice(0, 10)} />
      {usage.rows.length === 0 && <Empty text={EMPTY_TEXT} />}
      <Tables body={usage} />
    </>
  );
}

export default function Usage() {
  const [params, setParams] = useSearchParams();
  const days = params.get("days") ?? "";
  const [answer, setAnswer] = useState<UsageAnswer | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    document.title = "usage";
  }, []);

  useEffect(() => {
    let live = true;
    setAnswer(null);
    setError("");
    getJson<UsageAnswer>(days === "" ? "/usage" : `/usage?days=${encodeURIComponent(days)}`)
      .then((body) => {
        if (live) {
          setAnswer(body);
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the usage did not load");
        }
      });
    return () => {
      live = false;
    };
  }, [days]);

  if (error !== "") {
    return (
      <Shell section="usage">
        <ErrorLine text={error} />
      </Shell>
    );
  }
  if (answer === null) {
    return (
      <Shell section="usage">
        <Spinner label="Loading the usage" />
      </Shell>
    );
  }
  return (
    <Shell section="usage">
      <Page title="usage">
        <Meters limits={answer.limits} configured={answer.limits_configured} />
        <div className="j-chips" role="group" aria-label="Window">
          {WINDOWS.map((span) => (
            <Chip
              key={span}
              on={span === answer.days}
              onClick={() => setParams({ days: `${span}` })}
            >
              {`${span} days`}
            </Chip>
          ))}
        </div>
        <Body answer={answer} />
      </Page>
    </Shell>
  );
}
