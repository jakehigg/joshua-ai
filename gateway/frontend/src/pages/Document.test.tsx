import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import Document, { type DocumentData } from "./Document";
import type { ShowcaseData } from "../assignments/data";
import type { DocComment } from "../comments/CommentCard";
import { SEND } from "../showcase/DecisionCard";
import type { Session } from "../shell/Shell";

const SESSION: Session = {
  person: { id: "alex", name: "Alex", role: "member" },
  anonymous: false,
  sections: [{ key: "documents", label: "Documents", path: "/documents" }],
  chat_url: "/chat/alex",
  timezone: "UTC",
};

const COMMENT: DocComment = {
  id: "1",
  line_no: 0,
  line_text: "title: Party plan",
  line_now: "title: Party plan",
  stale: false,
  body: "Add the time.",
  author: "alex",
  created_at: "2026-11-09T12:00:00+00:00",
  resolved_at: null,
  resolution: null,
};

const DOC: DocumentData = {
  path: "documents/alex/party.md",
  url: "/documents/alex/party",
  title: "Party plan",
  kind: "plan",
  status: "active",
  format: "markdown",
  summary: "The plan for the party on Saturday.",
  updated: "2026-11-09T12:00:00+00:00",
  owner: "alex",
  owner_label: "yours",
  frontmatter: {
    title: "Party plan",
    kind: "plan",
    status: "active",
    summary: "The plan for the party on Saturday.",
    assignment: 12,
  },
  artifact_url: null,
  blocks: [
    {
      id: "L1",
      kind: "p",
      depth: 0,
      markdown: "Food and music for twenty people.",
      first_line: 1,
      last_line: 1,
    },
    { id: "L3", kind: "li", depth: 0, markdown: "- Order the food", first_line: 3, last_line: 3 },
  ],
  report: null,
  lines: ["Food and music for twenty people.", "", "- Order the food"],
  preamble: ["---", "title: Party plan", "kind: plan", "---"],
  comments: [COMMENT],
  versions: [
    { sha: "aaaaaaaabbbb", when: "2026-11-09T15:00:00+00:00", message: "write_document: party" },
    { sha: "ccccccccdddd", when: "2026-11-08T15:00:00+00:00", message: "write_document: party" },
  ],
  version: null,
  csrf: "a-token",
};

const HTML_DOC: DocumentData = {
  ...DOC,
  title: "Mockup",
  kind: "artifact",
  format: "html",
  url: "/documents/alex/mockup",
  path: "documents/alex/mockup.html",
  frontmatter: { title: "Mockup", kind: "artifact" },
  artifact_url: "/documents/alex/mockup/artifact",
  blocks: [],
  lines: ['<h1>Hi "there"</h1>'],
  comments: [],
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The document, the session, and one answer of a write. */
function stubDoc(doc: Response = json(DOC)) {
  const asked: string[] = [];
  vi.stubGlobal("fetch", (path: string) => {
    asked.push(path);
    if (path.startsWith("/api/v1/documents/")) {
      return Promise.resolve(doc.clone());
    }
    return Promise.resolve(json(SESSION));
  });
  return asked;
}

function page(address = "/documents/alex/party") {
  return render(
    <MemoryRouter initialEntries={[address]}>
      <Routes>
        <Route path="/documents/:owner/:slug" element={<Document />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** The body of the fold whose summary says `title`. */
function fold(title: string): HTMLElement {
  const summary = screen.getByText(title);
  return summary.closest("details")?.querySelector(".a-fold-body") as HTMLElement;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the head of a document", () => {
  it("gives the title, the marks, the properties and the + of the document", async () => {
    stubDoc();

    page();

    expect(await screen.findByText("Party plan")).toBeDefined();
    // The comment count arrives with a second call; wait for it, or a slow
    // run reads the head before it is there.
    await screen.findByText("1 comment for Joshua");
    const head = document.querySelector("header.d-hero") as HTMLElement;
    expect(head.id).toEqual("L0");
    expect(head.querySelector("h1")?.textContent).toEqual("Party plan");
    expect(head.querySelector(".j-badge.accent")?.textContent).toEqual("active");
    expect(head.querySelector(".d-kind")?.textContent).toEqual("plan");
    expect(head.querySelector(".a-comments")?.textContent).toEqual("1 comment for Joshua");
    expect(head.querySelector("time")?.getAttribute("datetime")).toEqual(
      "2026-11-09T12:00:00+00:00",
    );
    expect([...head.querySelectorAll(".a-prop-key")].map((item) => item.textContent)).toEqual([
      "summary",
      "assignment",
    ]);
    expect(head.querySelector(".a-prop-value")?.textContent).toEqual(
      "The plan for the party on Saturday.",
    );
    expect(screen.getByTitle("Comment on the document")).toBeDefined();
    expect(document.title).toEqual("Party plan");
  });

  it("shows the comment on line 0 under the head, and links the assignment", async () => {
    stubDoc();

    page();

    await screen.findByText("Add the time.");
    const head = document.querySelector("header.d-hero.has-comments") as HTMLElement;
    expect(head.querySelector(".a-comment-body")?.textContent).toEqual("Add the time.");
    const link = screen.getByText("assignment 12");
    expect(link.getAttribute("href")).toEqual("/assignments/12");
  });

  it("gives no second title when the body starts with one", async () => {
    stubDoc(json({ ...DOC, lines: ["# Party plan", "", "Food."] }));

    page();

    await screen.findByText("Food and music for twenty people.");
    expect(document.querySelector("header.d-hero h1")).toBeNull();
  });

  it("names the folder of the document in the crumbs", async () => {
    stubDoc();

    page();

    await screen.findByText("Party plan");
    const crumbs = document.querySelector("nav.j-crumbs") as HTMLElement;
    expect(crumbs.textContent).toContain("yours");
    expect(crumbs.querySelector("a")?.getAttribute("href")).toEqual("/documents");
  });
});

describe("the body of a document", () => {
  it("renders the blocks, each one a unit of the gutter", async () => {
    stubDoc();

    page();

    await screen.findByText("Party plan");
    expect(document.querySelector("#L1 p")?.textContent).toEqual(
      "Food and music for twenty people.",
    );
    expect(document.querySelector("#L3 ul li")?.textContent).toEqual("Order the food");
    expect(screen.getByTitle("Comment on the paragraph at line 1")).toBeDefined();
    expect(screen.getByTitle("Comment on the list item at line 3")).toBeDefined();
  });

  it("folds the source: the frontmatter with no +, then each line with one", async () => {
    stubDoc();

    page();

    await screen.findByText("Party plan");
    const source = fold("Source");
    expect(screen.getByText("3 lines")).toBeDefined();
    expect(source.querySelectorAll("li.a-pre").length).toEqual(4);
    expect(source.querySelectorAll("li.a-pre button").length).toEqual(0);
    expect(source.querySelectorAll("li.a-line:not(.a-pre)").length).toEqual(3);
    expect(screen.getByTitle("Comment on line 3")).toBeDefined();
    expect(source.querySelector("li#S1 .a-code")?.textContent).toEqual(
      "Food and music for twenty people.",
    );
  });

  it("folds the versions, and links an older one with its revision", async () => {
    stubDoc();

    page();

    await screen.findByText("Party plan");
    const versions = fold("Versions");
    const links = [...versions.querySelectorAll("ol.d-versions a")];
    expect(screen.getByText("2 versions")).toBeDefined();
    expect(links[0]?.getAttribute("href")).toEqual("/documents/alex/party");
    expect(links[0]?.textContent).toContain("(now)");
    expect(links[0]?.textContent).toContain("2026-11-09 15:00");
    expect(links[1]?.getAttribute("href")).toEqual("/documents/alex/party?rev=ccccccccdddd");
    expect(links[1]?.querySelector("code")?.textContent).toEqual("cccccccc");
  });

  it("says a document has no version history when it has none", async () => {
    stubDoc(json({ ...DOC, versions: [] }));

    page();

    expect(await screen.findByText("No version history.")).toBeDefined();
  });
});

describe("an older version", () => {
  it("asks for the revision, says it is read-only, and shows no +", async () => {
    const asked = stubDoc(
      json({
        ...DOC,
        version: { sha: "ccccccccdddd", when: "2026-11-08T15:00:00+00:00" },
        comments: [],
        csrf: null,
      }),
    );

    page("/documents/alex/party?rev=ccccccccdddd");

    expect(await screen.findByText("The version of 2026-11-08 15:00")).toBeDefined();
    expect(asked).toContain("/api/v1/documents/alex/party?rev=ccccccccdddd");
    expect(document.querySelector("aside.d-old")).not.toBeNull();
    expect(document.querySelector("button.v-plus")).toBeNull();
    expect(screen.getByText("Read the current version").getAttribute("href")).toEqual(
      "/documents/alex/party",
    );
  });
});

describe("an HTML document", () => {
  it("shows the artifact route in a sandboxed frame, with no script", async () => {
    stubDoc(json(HTML_DOC));

    page("/documents/alex/mockup");

    await screen.findByText("Mockup");
    const frame = document.querySelector("iframe.d-frame") as HTMLIFrameElement;
    expect(frame.getAttribute("src")).toEqual("/documents/alex/mockup/artifact");
    expect(frame.getAttribute("sandbox")).toEqual("allow-same-origin");
    expect(frame.getAttribute("referrerpolicy")).toEqual("no-referrer");
    expect(frame.getAttribute("title")).toEqual("Mockup");
    // The note is the ⓘ of the bar, not a line of its own.
    expect(screen.getByRole("img", { name: "Scripts of this document do not run." })).toBeDefined();
    expect(document.querySelector(".d-frame-note")).toBeNull();
    expect(document.querySelector(".a-blocks")).toBeNull();
  });

  it("allows the scripts of a document that asks for them", async () => {
    stubDoc(
      json({ ...HTML_DOC, frontmatter: { ...HTML_DOC.frontmatter, scripts: true } }),
    );

    page("/documents/alex/mockup");

    await screen.findByText("Mockup");
    expect(document.querySelector("iframe.d-frame")?.getAttribute("sandbox")).toEqual(
      "allow-scripts",
    );
    expect(
      screen.getByRole("img", { name: "Scripts of this document run in a sandbox, apart from the viewer." }),
    ).toBeDefined();
  });
});

describe("a document that is not there", () => {
  it("says what the viewer said", async () => {
    stubDoc(json({ error: "not found" }, 404));

    page("/documents/mia/secret");

    const line = await screen.findByText("not found");
    expect(line.className).toEqual("j-error");
    expect(document.querySelector("header.d-hero")).toBeNull();
  });
});

const YAML = "```yaml\nserver:\n  name: web\nlogger:\n  level: DEBUG\n```";

const CODE_DOC: DocumentData = {
  ...DOC,
  title: "Server settings",
  kind: "artifact",
  blocks: [
    { id: "L1", kind: "p", depth: 0, markdown: "Set it once.", first_line: 1, last_line: 1 },
    { id: "L3", kind: "code", depth: 0, markdown: YAML, first_line: 3, last_line: 8 },
  ],
  lines: ["Set it once.", "", "```yaml", "server:", "  name: web", "logger:", "  level: DEBUG", "```"],
};

describe("the embed mode", () => {
  it("shows no breadcrumb, no head card and no app bar, only the body", async () => {
    stubDoc();
    // The browser address carries the flag, which the shell reads.
    window.history.replaceState({}, "", "/documents/alex/party?embed=1");
    try {
      page("/documents/alex/party?embed=1");
      await screen.findByText("Order the food");
      expect(document.querySelector("header.j-appbar")).toBeNull();
      expect(document.querySelector("aside.j-sidebar")).toBeNull();
    } finally {
      window.history.replaceState({}, "", "/");
    }
    expect(document.querySelector(".j-crumbs")).toBeNull();
    expect(document.querySelector("header.d-hero")?.closest("details")).not.toBeNull();
    expect(document.querySelector(".d-embed")).not.toBeNull();
  });

  it("keeps the metadata, the source and the versions in a closed Details fold", async () => {
    stubDoc();
    page("/documents/alex/party?embed=1");
    await screen.findByText("Order the food");
    const details = screen.getByText("Details").closest("details") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details.textContent).toContain("active");
    expect(details.textContent).toContain("The plan for the party on Saturday.");
    expect(details.textContent).toContain("Versions");
    expect(details.textContent).toContain("Source");
  });

  it("keeps the comment gutter on the blocks", async () => {
    stubDoc();
    page("/documents/alex/party?embed=1&rev=aaaaaaaabbbb");
    await screen.findByText("Order the food");
    expect(document.querySelectorAll(".d-body .a-block").length).toBe(2);
    expect(document.querySelector(".d-body .a-block [title^='Comment on'], .d-body .a-block button")).not.toBeNull();
  });

  it("shows a body that is mostly one code block code-first, with Copy, and the prose in Notes", async () => {
    stubDoc(json(CODE_DOC));
    page("/documents/alex/party?embed=1");
    const copy = await screen.findByRole("button", { name: "copy" });
    expect(copy).toBeTruthy();
    const body = document.querySelector(".d-embed") as HTMLElement;
    const order = [...body.children].map((el) => el.className);
    expect(order[0]).toContain("d-codefirst");
    const notes = screen.getByText("Notes").closest("details") as HTMLDetailsElement;
    expect(notes.open).toBe(false);
    expect(notes.textContent).toContain("Set it once.");
  });
});

describe("an HTML document in embed mode", () => {
  it("keeps the note on scripts in the Details fold, not as a line under the frame", async () => {
    stubDoc(json(HTML_DOC));
    page("/documents/alex/mockup?embed=1");
    await screen.findByText("Mockup");
    const note = screen.getByText("Scripts of this document do not run.");
    expect(note.closest("details")).not.toBeNull();
    expect((note.closest("details") as HTMLDetailsElement).textContent).toContain("Details");
    // The frame is a direct part of the page, with no note after it.
    expect(document.querySelector(".d-artifact + .d-frame-note")).toBeNull();
    expect(document.querySelector(".d-artifact .d-frame-note")).toBeNull();
    expect(document.querySelector(".d-artifact .d-info")).toBeNull();
  });
});

describe("code on the document page", () => {
  it("is highlighted, on the full page too", async () => {
    stubDoc(json(CODE_DOC));
    page();
    await screen.findByText("Server settings");
    expect(document.querySelector("pre code.language-yaml .hljs-attr")).not.toBeNull();
    expect(document.querySelector(".j-crumbs")).not.toBeNull();
  });
});

/** The body of a document that has the headings of a report. */
const REPORT_LINES = [
  "# Party plan",
  "",
  "Twenty people come on Saturday.",
  "",
  "## Highlights",
  "",
  "- **The tent is booked.** It holds twenty.",
  "",
  "## Decisions for you",
  "",
  "1. **Buy the big tent?** It costs 200.",
];

/** The report of that body, as `report_json` sends it. */
const REPORT: ShowcaseData = {
  structured: true,
  title: "Party plan",
  lede: "Twenty people come on Saturday.",
  generated: false,
  first_section: 5,
  preamble: null,
  turns: null,
  extra: [],
  sections: {
    highlights: {
      key: "highlights",
      title: "Highlights",
      body: "- **The tent is booked.** It holds twenty.",
      line: 5,
      start: 7,
      end: 8,
      empty: false,
      items: [
        {
          number: 1,
          text: "**The tent is booked.** It holds twenty.",
          lead: "The tent is booked.",
          body: "It holds twenty.",
          line: 0,
          first_line: 7,
          last_line: 8,
          question: "The tent is booked.",
          context: "It holds twenty.",
          copy: "",
        },
      ],
      outputs: [],
      rest: "",
      blocks: [
        {
          id: "L7",
          kind: "li",
          depth: 0,
          markdown: "- **The tent is booked.** It holds twenty.",
          first_line: 7,
          last_line: 7,
        },
      ],
    },
    "decisions for you": {
      key: "decisions for you",
      title: "Decisions for you",
      body: "1. **Buy the big tent?** It costs 200.",
      line: 9,
      start: 11,
      end: 12,
      empty: false,
      items: [
        {
          number: 1,
          text: "**Buy the big tent?** It costs 200.",
          lead: "Buy the big tent?",
          body: "It costs 200.",
          line: 0,
          first_line: 11,
          last_line: 12,
          question: "Buy the big tent?",
          context: "It costs 200.",
          copy: "Document documents/alex/party.md, decision 1: Buy the big tent?",
        },
      ],
      outputs: [],
      rest: "",
      blocks: [
        {
          id: "L11",
          kind: "li",
          depth: 0,
          markdown: "1. **Buy the big tent?** It costs 200.",
          first_line: 11,
          last_line: 11,
        },
      ],
    },
  },
};

const REPORT_DOC: DocumentData = {
  ...DOC,
  kind: "report",
  blocks: [
    {
      id: "L3",
      kind: "p",
      depth: 0,
      markdown: "Twenty people come on Saturday.",
      first_line: 3,
      last_line: 3,
    },
  ],
  lines: REPORT_LINES,
  report: REPORT,
  comments: [],
};

describe("a document whose body is a report", () => {
  it("shows the showcase: the lede and the numbers in the head, the highlights as cards", async () => {
    stubDoc(json(REPORT_DOC));

    page();

    expect(await screen.findByText("The tent is booked.")).toBeDefined();
    const head = document.querySelector("header.d-hero") as HTMLElement;
    expect(head.querySelector("h1")?.textContent).toEqual("Party plan");
    expect(head.querySelector(".a-lede")?.textContent).toEqual("Twenty people come on Saturday.");
    expect(head.querySelector(".j-stats")?.textContent).toEqual("1decision");
    expect(document.querySelector(".a-cards.a-hls .a-hl")?.id).toEqual("L7");
    expect(document.querySelector(".a-blocks.d-body")).toBeNull();
  });

  it("gives each decision a copy button, and no script of the page", async () => {
    stubDoc(json(REPORT_DOC));

    page();

    const card = (await screen.findByText("Buy the big tent?")).closest("li") as HTMLElement;
    expect(card.id).toEqual("L11");
    expect(card.querySelector(".a-n")?.textContent).toEqual("1");
    expect(card.querySelector("button.a-copy")?.textContent).toEqual("Copy");
    expect(screen.queryByText(SEND)).toBeNull();
    expect(document.querySelectorAll("script").length).toEqual(0);
  });
});
