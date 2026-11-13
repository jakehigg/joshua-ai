// Joshua's messages as Markdown: GFM (tables, task lists, strikethrough,
// autolinks), sanitized, then highlighted.
//
// rehype-sanitize runs first, with the default (GitHub) schema. It keeps a
// `language-*` class on `code` and removes every script, event handler, and
// unsafe URL. rehype-highlight runs on the clean tree; its `hljs` spans come
// from the code text, so they need no second sanitize. Raw HTML in the
// Markdown is not rendered at all (there is no rehype-raw). A fenced block
// whose info string is `speech` (the spoken version) or `artifact` (the path
// of a document the turn wrote) renders as nothing; Message.tsx shows each
// of them in its own way.

import { memo, useRef, useState, type ComponentPropsWithoutRef } from "react";
import ReactMarkdown, { type Components, type ExtraProps, type Options } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { ARTIFACT_INFO, SPEECH_INFO } from "./blocks";

const remarkPlugins = [remarkGfm];
const rehypePlugins: Options["rehypePlugins"] = [rehypeSanitize, [rehypeHighlight, { detect: false }]];

function CodeBlock(props: ComponentPropsWithoutRef<"pre">) {
  const ref = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);
  const copy = () => {
    const text = ref.current?.textContent ?? "";
    try {
      void navigator.clipboard
        ?.writeText(text)
        .then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        })
        .catch(() => undefined);
    } catch {
      // No clipboard in this context (plain HTTP, old browser): do nothing.
    }
  };
  return (
    <div className="code-block">
      <button type="button" className="copy" onClick={copy} aria-label="Copy code">
        {copied ? "Copied" : "Copy"}
      </button>
      <pre ref={ref} {...props} />
    </div>
  );
}

const HIDDEN_CLASSES = [`language-${SPEECH_INFO}`, `language-${ARTIFACT_INFO}`];

// True for a `pre` whose code is a block that is not printed: a `speech` or
// an `artifact` block (class `language-speech` or `language-artifact`).
function isHiddenBlock(node: ExtraProps["node"]): boolean {
  const code = node?.children.find((c) => c.type === "element" && c.tagName === "code");
  if (!code || code.type !== "element") return false;
  const cls = code.properties.className;
  const names = Array.isArray(cls) ? cls.map(String) : String(cls ?? "").split(" ");
  return names.some((n) => HIDDEN_CLASSES.includes(n.toLowerCase()));
}

const components: Components = {
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
  pre: ({ node, ...props }) => (isHiddenBlock(node) ? null : <CodeBlock {...props} />),
  table: ({ node: _node, ...props }) => (
    <div className="table-wrap">
      <table {...props} />
    </div>
  ),
};

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        rehypePlugins={rehypePlugins}
        components={components}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
