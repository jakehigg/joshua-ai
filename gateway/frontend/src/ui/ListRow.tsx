import type { ReactNode } from "react";

/**
 * One row of a `j-list`: a title, an optional second line under it, and an
 * optional mark on the right. With `href` the whole row is a link.
 */
export default function ListRow({
  title,
  sub,
  href,
  side,
}: {
  title: string;
  sub?: string;
  href?: string;
  side?: ReactNode;
}) {
  const body = (
    <>
      <span className="j-list-main">
        <span className="j-list-title">{title}</span>
        {sub !== undefined && <span className="j-list-sub">{sub}</span>}
      </span>
      {side}
    </>
  );
  return (
    <li>
      {href === undefined ? (
        <div className="j-list-row">{body}</div>
      ) : (
        <a className="j-list-row" href={href}>
          {body}
        </a>
      )}
    </li>
  );
}
