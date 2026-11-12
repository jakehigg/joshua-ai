/**
 * The links group of the sidebar: one row per link, then the way to edit them.
 *
 * The list comes from `GET /api/v1/links`, the one source of it, and the
 * `/links` page reads the same address. Each link opens in a new tab, with
 * `rel="noopener noreferrer"`, because it leaves the viewer. The icon is a
 * built-in one (`LINK_ICONS`): a link brings no icon of its own, because the
 * content policy of the viewer allows an image from this origin only.
 *
 * The group is not the page: when the list does not load, the sidebar still
 * shows the way to the page and the page says what went wrong.
 */

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { getJson } from "../api/client";
import Icon from "./Icon";
import { LINKS_PATH, LINKS_SECTION, linkIcon } from "./sections";

/** One link of the list, as `GET /api/v1/links` sends it. */
export interface LinkRow {
  title: string;
  url: string;
  icon: string;
}

/** The answer of `GET /api/v1/links`. */
export interface LinksData {
  links: LinkRow[];
  icons: string[];
  can_edit: boolean;
  csrf: string | null;
}

export default function LinksNav({ section = "" }: { section?: string }) {
  const [links, setLinks] = useState<LinkRow[]>([]);

  useEffect(() => {
    let live = true;
    getJson<LinksData>("/links")
      .then((answer) => {
        if (live) {
          setLinks(answer.links ?? []);
        }
      })
      .catch(() => {
        // The sidebar is not the page: with no list it still offers the page.
      });
    return () => {
      live = false;
    };
  }, []);

  return (
    <>
      <div className="j-label">Links</div>
      <nav className="v-sections v-links" aria-label="Links">
        {links.map((link) => (
          <a
            className="j-nav-item"
            key={link.url}
            href={link.url}
            target="_blank"
            rel="noopener noreferrer"
          >
            <Icon paths={linkIcon(link.icon)} />
            <span>{link.title}</span>
          </a>
        ))}
        <Link
          className="j-nav-item sub v-links-edit"
          to={LINKS_PATH}
          aria-current={section === LINKS_SECTION ? "page" : undefined}
        >
          {links.length > 0 ? "Edit links" : "Add links"}
        </Link>
      </nav>
    </>
  );
}
