import type { ReactNode } from "react";

/**
 * One small mark beside a title, or a small button. With `href` it is a link,
 * with `onClick` it is a button, and `on` marks the one that is chosen.
 */
export default function Chip({
  children,
  href,
  on,
  onClick,
}: {
  children: ReactNode;
  href?: string;
  on?: boolean;
  onClick?: () => void;
}) {
  const className = on ? "j-chip on" : "j-chip";
  if (href !== undefined) {
    return (
      <a className={className} href={href}>
        {children}
      </a>
    );
  }
  return (
    <button type="button" className={className} aria-pressed={on} onClick={onClick}>
      {children}
    </button>
  );
}
