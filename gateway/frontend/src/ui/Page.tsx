import type { ReactNode } from "react";

/** A page of the viewer: the title, then the body. */
export default function Page({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <>
      <h1 className="j-doc-title">{title}</h1>
      {children}
    </>
  );
}
