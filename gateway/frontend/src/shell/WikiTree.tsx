import { useEffect, useState } from "react";

import { ApiError, getJson } from "../api/client";
import Empty from "../ui/Empty";
import ErrorLine from "../ui/ErrorLine";
import Spinner from "../ui/Spinner";

/** One node of `GET /api/v1/wiki/tree`: a folder, or a page with no child. */
export interface TreeNode {
  title: string;
  rel: string;
  url: string;
  count: number;
  children: TreeNode[];
}

/** The shipped docs are closed, because a person did not write them. */
export const CLOSED_DIRS = new Set(["joshua-docs"]);
/** A folder with more pages than this starts closed. */
export const OPEN_MAX_PAGES = 12;
/** The address of the journal, which is a feed and not a tree. */
export const JOURNAL_URL = "/journal";

/** True for a page: a page is a Markdown file and holds no child. */
export function isPage(node: TreeNode): boolean {
  return node.rel.endsWith(".md");
}

function folderName(node: TreeNode): string {
  return node.rel.split("/").pop() ?? "";
}

function isOpen(node: TreeNode, depth: number): boolean {
  return depth === 0 && !CLOSED_DIRS.has(folderName(node)) && node.count <= OPEN_MAX_PAGES;
}

function PageItem({ node }: { node: TreeNode }) {
  return (
    <li>
      <a href={node.url}>{node.title}</a>
    </li>
  );
}

function FolderItem({ node, depth }: { node: TreeNode; depth: number }) {
  if (node.url === JOURNAL_URL) {
    return (
      <li>
        <a href={JOURNAL_URL}>journal</a> <span className="count">{node.count} pages</span>
      </li>
    );
  }
  return (
    <li>
      <details open={isOpen(node, depth)}>
        <summary>
          <a href={node.url}>{node.title}</a> <span className="count">{node.count}</span>
        </summary>
        <ul>
          {node.children.map((child) =>
            isPage(child) ? (
              <PageItem key={child.rel} node={child} />
            ) : (
              <FolderItem key={child.rel} node={child} depth={depth + 1} />
            ),
          )}
        </ul>
      </details>
    </li>
  );
}

/**
 * The tree of the wiki: the front page, the journal as one link, each folder
 * as a block that opens, then the loose pages of the root.
 */
export function Tree({ root }: { root: TreeNode }) {
  const children = root.children;
  const first = children[0];
  const front = first !== undefined && isPage(first) ? first : undefined;
  const rest = front === undefined ? children : children.slice(1);
  const folders = rest.filter((node) => !isPage(node));
  const pages = rest.filter((node) => isPage(node));
  if (front === undefined && folders.length === 0 && pages.length === 0) {
    return <Empty />;
  }
  return (
    <ul className="tree">
      {front !== undefined && (
        <li>
          <a href={front.url}>{front.title}</a> <span className="count">the front page</span>
        </li>
      )}
      {folders.map((node) => (
        <FolderItem key={node.rel} node={node} depth={0} />
      ))}
      {pages.length > 0 && (
        <li>
          <details>
            <summary>
              other pages <span className="count">{pages.length}</span>
            </summary>
            <ul>
              {pages.map((node) => (
                <PageItem key={node.rel} node={node} />
              ))}
            </ul>
          </details>
        </li>
      )}
    </ul>
  );
}

/** The tree of the wiki, read from the viewer. */
export default function WikiTree() {
  const [root, setRoot] = useState<TreeNode | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let live = true;
    getJson<TreeNode>("/wiki/tree")
      .then((answer) => {
        if (live) {
          setRoot(answer);
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the wiki tree did not load");
        }
      });
    return () => {
      live = false;
    };
  }, []);

  if (error !== "") {
    return <ErrorLine text={error} />;
  }
  if (root === null) {
    return <Spinner label="Loading the wiki" />;
  }
  return <Tree root={root} />;
}
