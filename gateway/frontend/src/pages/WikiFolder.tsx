import { useEffect } from "react";

import Markdown, { wikiRewriter } from "../md/Markdown";
import Shell from "../shell/Shell";
import Empty from "../ui/Empty";
import Page from "../ui/Page";
import { Crumbs, HopLink, type Hop } from "./WikiPage";

/** One file of the folder that is not a page. */
export interface FolderFile {
  name: string;
  url: string;
}

/** The answer of `GET /api/v1/wiki` for a folder of the wiki. */
export interface WikiFolderData {
  kind: "folder";
  rel: string;
  title: string;
  crumbs: Hop[];
  index_markdown: string | null;
  folders: Hop[];
  pages: Hop[];
  files: FolderFile[];
  can_create: boolean;
  new_url: string;
}

/** A list of addresses under one label. A folder keeps its trailing slash. */
function Hops({ label, items }: { label: string; items: Hop[] }) {
  if (items.length === 0) {
    return null;
  }
  return (
    <>
      <h2 className="j-label">{label}</h2>
      <ul className="tree">
        {items.map((item) => (
          <li key={item.url}>
            <HopLink to={item.url}>
              {item.url.endsWith("/") ? `${item.title}/` : item.title}
            </HopLink>
          </li>
        ))}
      </ul>
    </>
  );
}

function Files({ items }: { items: FolderFile[] }) {
  if (items.length === 0) {
    return null;
  }
  return (
    <>
      <h2 className="j-label">files</h2>
      <ul className="tree">
        {items.map((item) => (
          <li key={item.url}>
            <a href={item.url}>{item.name}</a>
          </li>
        ))}
      </ul>
    </>
  );
}

/**
 * The folder of the index page, as an address of the app.
 *
 * The index page of a folder sits beside it, one folder up, so its relative
 * links resolve there: `recipes/sauces.md` is the index page of
 * `recipes/sauces/` and reads its links from `/wiki/recipes/`.
 */
function indexFolder(rel: string): string {
  const cut = rel.lastIndexOf("/");
  return cut < 0 ? "/wiki/" : `/wiki/${rel.slice(0, cut + 1)}`;
}

/** `new page in Recipes`, or `new page in the wiki` at the root. */
function where(rel: string): string {
  const name = rel.split("/").pop() ?? "";
  return name === "" ? "the wiki" : name.replace(/[-_]/g, " ");
}

/**
 * One folder of the wiki: the crumbs, the title, the body of its index page,
 * then the subfolders, the pages and the files. A member also gets the link
 * that starts a new page here.
 */
export default function WikiFolder({ folder }: { folder: WikiFolderData }) {
  useEffect(() => {
    document.title = folder.title;
  }, [folder.title]);

  const empty =
    folder.folders.length === 0 && folder.pages.length === 0 && folder.files.length === 0;
  return (
    <Shell section="wiki">
      <Crumbs items={folder.crumbs} />
      <Page title={folder.title}>
        {folder.index_markdown !== null && folder.index_markdown !== "" && (
          <Markdown
            text={folder.index_markdown}
            rewriteUrl={wikiRewriter(indexFolder(folder.rel))}
          />
        )}
        <Hops label="folders" items={folder.folders} />
        <Hops label="pages" items={folder.pages} />
        <Files items={folder.files} />
        {empty && <Empty text="nothing here yet." />}
        {folder.can_create && (
          <div className="v-actions">
            <a className="j-btn sm" href={folder.new_url}>
              new page in {where(folder.rel)}
            </a>
          </div>
        )}
      </Page>
    </Shell>
  );
}
