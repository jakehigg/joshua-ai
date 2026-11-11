import { describe, expect, it } from "vitest";

import {
  DEFAULT_LINK_ICON,
  LINK_ICONS,
  SECTIONS,
  linkIcon,
  section,
} from "./sections";

describe("the section table", () => {
  it("holds the sections of the viewer in the order of the sidebar", () => {
    expect(SECTIONS.map((item) => item.key)).toEqual([
      "wiki",
      "journal",
      "search",
      "usage",
      "assignments",
      "documents",
      "chat",
    ]);
  });

  it("gives each section an address and the path data of an icon", () => {
    for (const item of SECTIONS) {
      expect(item.path.startsWith("/")).toBe(true);
      expect(item.icon).toContain("<path");
      expect(item.label).not.toEqual("");
    }
  });

  it("keeps Documents for a member and the chat on the person", () => {
    expect(SECTIONS.filter((item) => item.memberOnly).map((item) => item.key)).toEqual([
      "documents",
    ]);
    expect(SECTIONS.filter((item) => item.personPath).map((item) => item.key)).toEqual(["chat"]);
    expect(section("chat")?.path).toEqual("/chat");
  });

  it("keeps the wiki as the one migrated section; the rest load a page", () => {
    expect(SECTIONS.filter((item) => item.migrated).map((item) => item.key)).toEqual(["wiki"]);
  });

  it("finds one section by key, and nothing for a name that is not one", () => {
    expect(section("wiki")?.label).toEqual("Wiki");
    expect(section("nope")).toBeUndefined();
  });
});

describe("the icons of the links", () => {
  it("draws a built-in icon by name", () => {
    expect(linkIcon("chat")).toEqual(LINK_ICONS["chat"]);
  });

  it("draws the link icon for a name that is not built in", () => {
    expect(linkIcon("nope")).toEqual(LINK_ICONS[DEFAULT_LINK_ICON]);
  });
});
