import { useEffect } from "react";
import { Link } from "react-router-dom";

import Shell from "../shell/Shell";
import Page from "../ui/Page";
import Users from "./admin/Users";

/** The section key of the admin pages, which the shell marks in its app bar. */
export const ADMIN_SECTION = "admin";

/** The address of the one admin page that is built. */
export const USERS_PATH = "/admin/users";

/**
 * The rest of the admin area, in the order the left nav shows it. Not one of
 * these is built: each is a static line marked "soon", so the owner sees what
 * the section will hold and taps nothing that does nothing.
 */
export const SOON = ["MCP servers", "Channels", "Instance", "Usage"];

/**
 * `/admin` and `/admin/users`: the admin area of the owner, in two columns.
 *
 * The left column is the nav of the area and the right one is the page. Only
 * Users is built, so it is the one link and the one page, and every other
 * line of the nav is a placeholder marked "soon".
 *
 * The route is the guard: `App` draws this page for the owner alone, and
 * every route under `/api/v1/admin/` refuses anybody else as well, so the
 * page never has to say who may see it.
 */
export default function Admin() {
  useEffect(() => {
    document.title = "admin";
  }, []);

  return (
    <Shell section={ADMIN_SECTION}>
      <Page title="Admin">
        <div className="v-admin">
          <nav className="v-admin-nav" aria-label="Admin sections">
            <Link className="j-nav-item active" to={USERS_PATH} aria-current="page">
              Users
            </Link>
            {SOON.map((label) => (
              <span className="j-nav-item v-admin-soon" key={label} aria-disabled="true">
                {label}
                <span className="j-chip sm">soon</span>
              </span>
            ))}
          </nav>
          <div className="v-admin-body">
            <Users />
          </div>
        </div>
      </Page>
    </Shell>
  );
}
