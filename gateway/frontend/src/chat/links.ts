// Which address an item may be a link to. A `url` of a workspace item can come
// from the agent or from a tool, so only an http(s) address and a path of the
// viewer become an href. Anything else (javascript:, data:, a bare word, a
// protocol-relative `//host`) is shown as plain text.

export function safeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  if (/^https?:\/\/[^\s]/i.test(url)) return url;
  if (url.startsWith("/") && !url.startsWith("//") && !url.startsWith("/\\")) return url;
  return null;
}
