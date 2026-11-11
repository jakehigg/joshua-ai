/**
 * The navigation of the viewer: the sections, and the icons of the links.
 *
 * The table is the one in `viewer.py` (`_SECTIONS`), so the two navigations
 * hold the same items in the same order. `memberOnly` is a section a guest
 * does not get. `personPath` is a section whose address ends with the id of
 * the person who signed in: the chat answers at `/chat/<person id>`.
 *
 * `migrated` says who renders the section. A migrated section is a route of
 * this app and renders as a router `<Link>`. A section that is not migrated
 * renders as a plain `<a>`: the browser loads the page, and the server
 * renderer of the viewer answers it. Each task of the rewrite migrates one
 * section and turns its flag.
 */

export interface Section {
  key: string;
  label: string;
  path: string;
  /** The path data of the icon, on a 24px grid. */
  icon: string;
  memberOnly: boolean;
  personPath: boolean;
  migrated: boolean;
}

const WIKI_ICON =
  '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/>' +
  '<path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>';
const JOURNAL_ICON =
  '<path d="M5 4h14a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/>' +
  '<path d="M8 2v4M16 2v4M3 10h18"/>';
const ASSIGNMENTS_ICON =
  '<path d="M9 4h6v3H9z"/>' +
  '<path d="M9 5H6a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1h-3"/>' +
  '<path d="m9 14 2 2 4-4"/>';
const DOCUMENTS_ICON =
  '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>' +
  '<path d="M14 3v5h5M9 13h6M9 17h6"/>';

/** The search icon, which the field in the app bar shows too. */
export const SEARCH_ICON = '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>';

/** The icon of "Your page". */
export const ME_ICON = '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>';

/** The icon of the chat, which is also a link icon. */
const CHAT_ICON = '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>';

export const SECTIONS: readonly Section[] = [
  {
    key: "wiki",
    label: "Wiki",
    path: "/wiki/",
    icon: WIKI_ICON,
    memberOnly: false,
    personPath: false,
    migrated: true,
  },
  {
    key: "journal",
    label: "Journal",
    path: "/journal",
    icon: JOURNAL_ICON,
    memberOnly: false,
    personPath: false,
    migrated: true,
  },
  {
    key: "search",
    label: "Search",
    path: "/search",
    icon: SEARCH_ICON,
    memberOnly: false,
    personPath: false,
    migrated: true,
  },
  {
    key: "usage",
    label: "Usage",
    path: "/usage",
    icon: '<path d="M4 20V11M10 20V4M16 20v-8M21 20H3"/>',
    memberOnly: false,
    personPath: false,
    migrated: true,
  },
  {
    key: "assignments",
    label: "Assignments",
    path: "/assignments",
    icon: ASSIGNMENTS_ICON,
    memberOnly: false,
    personPath: false,
    migrated: false,
  },
  {
    key: "documents",
    label: "Documents",
    path: "/documents",
    icon: DOCUMENTS_ICON,
    memberOnly: true,
    personPath: false,
    migrated: false,
  },
  {
    key: "chat",
    label: "Chat",
    path: "/chat",
    icon: CHAT_ICON,
    memberOnly: false,
    personPath: true,
    migrated: false,
  },
];

/** The section of `key`, or undefined when there is none. */
export function section(key: string): Section | undefined {
  return SECTIONS.find((item) => item.key === key);
}

/** The name of the icon a link falls back to. */
export const DEFAULT_LINK_ICON = "link";

/**
 * The built-in icons of the links in the sidebar, the set of
 * `viewer_links.ICONS`. A link brings no icon of its own, because the content
 * policy of the viewer allows an image from this origin only.
 */
export const LINK_ICONS: Record<string, string> = {
  link:
    '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
  box:
    '<path d="m3 7 9-4 9 4-9 4z"/><path d="M3 7v10l9 4 9-4V7M12 11v10"/>',
  list:
    '<path d="M9 6h11M9 12h11M9 18h11"/><path d="m3.5 6 1 1 2-2M3.5 12l1 1 2-2M3.5 18l1 1 2-2"/>',
  cart:
    '<path d="M3 4h2l2.5 11h10L20 8H6.5"/><circle cx="9" cy="19" r="1.3"/><circle cx="17" cy="19" r="1.3"/>',
  chart:
    '<path d="M4 20V11M10 20V4M16 20v-8M21 20H3"/>',
  home:
    '<path d="m3 11 9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>',
  music:
    '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
  calendar:
    '<path d="M5 4h14a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/><path d="M8 2v4M16 2v4M3 10h18"/>',
  book:
    '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
  tool:
    '<path d="M14.7 6.3a4 4 0 0 0 5 5L21 13l-8 8-3-3 6.7-6.7a4 4 0 0 1-5-5z"/><path d="m3 21 6-6"/>',
  bolt:
    '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
  cloud:
    '<path d="M7 18a5 5 0 1 1 .9-9.9A6 6 0 0 1 19 10a4 4 0 0 1 0 8z"/>',
  server:
    '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  camera:
    '<path d="M4 7h3l2-3h6l2 3h3a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="4"/>',
  chat: CHAT_ICON,
  heart:
    '<path d="M12 20s-7-4.4-9-9a5 5 0 0 1 9-3 5 5 0 0 1 9 3c-2 4.6-9 9-9 9z"/>',
  code:
    '<path d="m8 8-5 4 5 4M16 8l5 4-5 4M14 4l-4 16"/>',
  star:
    '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1 6.2L12 17.3 6.5 20.2l1-6.2L3 9.6l6.2-.9z"/>',
  globe:
    '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  play:
    '<circle cx="12" cy="12" r="9"/><path d="m10 8 6 4-6 4z"/>',
};

/** The path data of a link icon. An unknown name draws the link icon. */
export function linkIcon(name: string): string {
  return LINK_ICONS[name] ?? LINK_ICONS[DEFAULT_LINK_ICON] ?? "";
}
