import { useEffect } from "react";
import { Link, useLocation } from "react-router-dom";

import Shell from "../shell/Shell";
import Page from "../ui/Page";
import Instance from "./admin/Instance";
import Users from "./admin/Users";

/** The section key of the admin pages, which the shell marks in its app bar. */
export const ADMIN_SECTION = "admin";

/** The addresses of the admin pages that are built. */
export const USERS_PATH = "/admin/users";
export const INSTANCE_PATH = "/admin/instance";

/**
 * The sections of the admin area, in the order the left nav shows them. A
 * section with a path is built and is a link; one without is a static line
 * marked "soon", so the owner sees what the section will hold and taps
 * nothing that does nothing.
 */
export const SECTIONS: { label: string; path?: string }[] = [
  { label: "Users", path: USERS_PATH },
  { label: "MCP servers" },
  { label: "Channels" },
  { label: "Instance", path: INSTANCE_PATH },
  { label: "Usage" },
];

/**
 * `/admin`, `/admin/users`, and `/admin/instance`: the admin area of the
 * owner, in two columns.
 *
 * The left column is the nav of the area and the right one is the page. Users
 * and Instance are built, and every other line of the nav is a placeholder
 * marked "soon".
 *
 * The route is the guard: `App` draws this page for the owner alone, and
 * every route under `/api/v1/admin/` refuses anybody else as well, so the
 * page never has to say who may see it.
 */
export default function Admin() {
  const { pathname } = useLocation();

  useEffect(() => {
    document.title = "admin";
  }, []);

  // `/admin` itself draws Users, so that is the page and the marked link
  // everywhere but on the Instance address.
  const instance = pathname === INSTANCE_PATH;
  const here = instance ? INSTANCE_PATH : USERS_PATH;

  return (
    <Shell section={ADMIN_SECTION}>
      <Page title="Admin">
        <div className="v-admin">
          <nav className="v-admin-nav" aria-label="Admin sections">
            {SECTIONS.map((section) =>
              section.path === undefined ? (
                <span className="j-nav-item v-admin-soon" key={section.label} aria-disabled="true">
                  {section.label}
                  <span className="j-chip sm">soon</span>
                </span>
              ) : (
                <Link
                  className={section.path === here ? "j-nav-item active" : "j-nav-item"}
                  key={section.label}
                  to={section.path}
                  aria-current={section.path === here ? "page" : undefined}
                >
                  {section.label}
                </Link>
              ),
            )}
          </nav>
          <div className="v-admin-body">{instance ? <Instance /> : <Users />}</div>
        </div>
      </Page>
    </Shell>
  );
}
