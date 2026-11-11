/**
 * The one Markdown renderer of the app.
 *
 * The plugins run in this order, and the order is the safety of the page:
 * `remarkGfm` for the tables, the task lists and the strikethrough, then
 * `rehypeSanitize` with the default GitHub schema, then `rehypeHighlight`
 * with `detect: false`. Sanitizing before highlighting keeps the classes the
 * highlighter adds, and there is no `rehype-raw`: the HTML in a page is text,
 * never markup. The plugin list is the one of the chat
 * (`joshua-chat/frontend/src/chat/Markdown.tsx`), so the two renderers agree.
 *
 * A code block gets a copy button. The output is one `<article class="j-prose">`,
 * the class the pages of the viewer use.
 *
 * `rewriteUrl` corrects the addresses of a page as it renders; the stored
 * page is never changed. `wikiRewriter` is the rule of the wiki.
 */

import { useRef, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components, type Options } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

const REMARK_PLUGINS: Options["remarkPlugins"] = [remarkGfm];
const REHYPE_PLUGINS: Options["rehypePlugins"] = [
  rehypeSanitize,
  [rehypeHighlight, { detect: false }],
];

/** Where a page stores a link to an attachment of the wiki. */
const STORED_ATTACHMENTS = "/attachments/";

/** Where this viewer delivers an attachment of the wiki. */
const WIKI_ATTACHMENTS = "/api/v1/files/wiki/attachments/";

const SCHEME = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;

function resolveFrom(folder: string, target: string): string {
  const base = folder.endsWith("/") ? folder : `${folder}/`;
  const root = `/${base.replace(/^\//, "").split("/")[0] ?? ""}/`;
  try {
    const url = new URL(target, `http://viewer${base}`);
    const path = `${url.pathname}${url.search}${url.hash}`;
    // A link that goes above the root of the page is not ours to correct.
    return path.startsWith(root) ? path : target;
  } catch {
    return target;
  }
}

/**
 * The rule of the wiki, for the page in `folder` (`/wiki/recipes/`).
 *
 * A page stores an attachment the way a wiki frontend that serves the wiki at
 * its root reads it: `/attachments/YYYY/MM/name`. Here that is the route that
 * delivers the file. A relative address resolves against the folder of the
 * page, because the body of an index page renders on the address of its
 * folder, one level down. Every other address stays as it is: one with a
 * scheme, an absolute one, and one that is only a fragment or a query.
 */
export function wikiRewriter(folder: string): (url: string) => string {
  return (url) => {
    if (url.startsWith(STORED_ATTACHMENTS)) {
      return WIKI_ATTACHMENTS + url.slice(STORED_ATTACHMENTS.length);
    }
    if (url === "" || url.startsWith("/") || url.startsWith("#") || url.startsWith("?")) {
      return url;
    }
    if (SCHEME.test(url)) {
      return url;
    }
    return resolveFrom(folder, url);
  };
}

/** One code block, with the button that copies its text. */
function CodeBlock({ children }: { children?: ReactNode }) {
  const block = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);

  function copy(): void {
    const text = block.current?.textContent ?? "";
    void navigator.clipboard?.writeText(text).catch(() => undefined);
    setCopied(true);
  }

  return (
    <div className="v-code">
      <button className="j-btn sm v-code-copy" type="button" onClick={copy}>
        {copied ? "copied" : "copy"}
      </button>
      <pre ref={block}>{children}</pre>
    </div>
  );
}

function componentsWith(rewrite: (url: string) => string): Components {
  return {
    pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
    a: ({ href, title, children }) => (
      <a href={href === undefined ? undefined : rewrite(href)} title={title}>
        {children}
      </a>
    ),
    img: ({ src, alt, title }) => (
      <img src={src === undefined ? undefined : rewrite(src)} alt={alt ?? ""} title={title} />
    ),
  };
}

const AS_IS = (url: string): string => url;

/** The body of a page, as prose. */
export default function Markdown({
  text,
  rewriteUrl = AS_IS,
}: {
  text: string;
  rewriteUrl?: (url: string) => string;
}) {
  return (
    <article className="j-prose">
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={componentsWith(rewriteUrl)}
      >
        {text}
      </ReactMarkdown>
    </article>
  );
}
