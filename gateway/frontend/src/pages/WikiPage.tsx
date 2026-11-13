import { Fragment, useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError, postJson } from "../api/client";
import Markdown, { wikiRewriter } from "../md/Markdown";
import Shell from "../shell/Shell";
import ErrorLine from "../ui/ErrorLine";
import type { SaveAnswer } from "./Edit";

/** One crumb above a page, or one neighbour of it. */
export interface Hop {
  title: string;
  url: string;
}

/** The answer of `GET /api/v1/wiki` for a Markdown file. */
export interface WikiPageData {
  kind: "page";
  root: string;
  rel: string;
  title: string;
  frontmatter: Record<string, string>;
  markdown: string;
  crumbs: Hop[];
  siblings: Hop[];
  can_edit: boolean;
  edit_url: string;
  delete_csrf: string;
}

/** The addresses this app renders itself: the content of the viewer. */
const APP_PATH = /^\/(?:(?:wiki|shared|attachments)\/.*)?$/;

/**
 * One address of the viewer. An address of this app is a router link, so the
 * browser loads no page; every other one is a plain link, which the browser
 * loads.
 */
export function HopLink({ to, children }: { to: string; children: ReactNode }) {
  if (APP_PATH.test(to)) {
    return <Link to={to}>{children}</Link>;
  }
  return <a href={to}>{children}</a>;
}

/** `home › wiki › Recipes › Ponzu Sauce`. A crumb with no address is here. */
export function Crumbs({ items }: { items: Hop[] }) {
  if (items.length === 0) {
    return null;
  }
  return (
    <p className="j-crumbs">
      {items.map((item, index) => (
        <Fragment key={`${item.url}:${item.title}`}>
          {index > 0 && " › "}
          {item.url === "" ? (
            <span>{item.title}</span>
          ) : (
            <HopLink to={item.url}>{item.title}</HopLink>
          )}
        </Fragment>
      ))}
    </p>
  );
}

/** One muted line of the frontmatter. Empty frontmatter shows nothing. */
function Meta({ front }: { front: Record<string, string> }) {
  const pairs = Object.entries(front);
  if (pairs.length === 0) {
    return null;
  }
  return (
    <p className="j-meta frontmatter">
      {pairs.map(([key, value], index) => (
        <Fragment key={key}>
          {index > 0 && " · "}
          <span>
            <span className="k">{key}:</span> {value}
          </span>
        </Fragment>
      ))}
    </p>
  );
}

/** The other pages of the folder, and the folders beside it. */
function Siblings({ items, folder }: { items: Hop[]; folder: string }) {
  if (items.length === 0) {
    return null;
  }
  return (
    <section className="siblings">
      <h2 className="j-label">more in {folder}</h2>
      <ul className="links">
        {items.map((item) => (
          <li key={item.url}>
            <HopLink to={item.url}>{item.title}</HopLink>
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * The delete, for a member on the wiki.
 *
 * The button asks first, then posts the page and its CSRF token to
 * `POST /api/v1/delete`, which moves the page under `wiki/.trash/` and says
 * where to go. It is never a hard delete.
 */
function Delete({ path, csrf }: { path: string; csrf: string }) {
  const navigate = useNavigate();
  const [error, setError] = useState<string>("");

  async function remove(): Promise<void> {
    if (!window.confirm("Move this page to the trash?")) {
      return;
    }
    try {
      const answer = await postJson<SaveAnswer>("/delete", { csrf, path });
      navigate(answer.url);
    } catch (exc: unknown) {
      setError(exc instanceof ApiError ? exc.message : "the page did not move");
    }
  }

  return (
    <>
      <button className="j-btn sm danger" type="button" onClick={remove}>
        move this page to trash
      </button>
      {error !== "" && <ErrorLine text={error} />}
    </>
  );
}

/** The folder of the page, as an address of the app: `/wiki/recipes/`. */
function folderOf(rel: string): string {
  const cut = rel.lastIndexOf("/");
  return cut < 0 ? "/" : `/${rel.slice(0, cut + 1)}`;
}

/** The title of the folder the page is in, the crumb above the page. */
function parentTitle(crumbs: Hop[]): string {
  return crumbs[crumbs.length - 2]?.title ?? "wiki";
}

/**
 * One page of the wiki, of the shared files, or of the own attachments: the
 * crumbs, the frontmatter, the prose, the other pages of the folder, and for
 * a member on the wiki the editor and the delete.
 */
export default function WikiPage({ page }: { page: WikiPageData }) {
  useEffect(() => {
    document.title = page.title;
  }, [page.title]);

  return (
    <Shell section={page.root === "wiki" ? "wiki" : ""}>
      <Crumbs items={page.crumbs} />
      <Meta front={page.frontmatter} />
      <Markdown text={page.markdown} rewriteUrl={wikiRewriter(folderOf(page.rel))} />
      <Siblings items={page.siblings} folder={parentTitle(page.crumbs)} />
      {page.can_edit && (
        <div className="v-actions">
          <a className="j-btn sm" href={page.edit_url}>
            edit this page
          </a>
          <Delete path={page.rel} csrf={page.delete_csrf} />
        </div>
      )}
    </Shell>
  );
}
