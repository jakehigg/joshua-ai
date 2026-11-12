import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import Fold from "./Fold";

function fold(props: { title: string; hint?: string; open?: boolean }) {
  return render(
    <Fold {...props}>
      <p>the lines</p>
    </Fold>,
  );
}

describe("a fold", () => {
  it("is a closed details with its title and its hint", () => {
    fold({ title: "Source", hint: "12 lines" });

    const details = document.querySelector("details.a-fold") as HTMLDetailsElement;
    expect(details).not.toBeNull();
    expect(details.open).toBe(false);
    expect(document.querySelector(".a-fold-title")?.textContent).toEqual("Source");
    expect(document.querySelector(".a-fold-hint")?.textContent).toEqual("12 lines");
    expect(document.querySelector(".a-fold-body p")?.textContent).toEqual("the lines");
  });

  it("gives no hint when there is none", () => {
    fold({ title: "Versions" });

    expect(document.querySelector(".a-fold-hint")).toBeNull();
    expect(screen.getByText("Versions")).toBeDefined();
  });

  it("opens when the caller asks", () => {
    fold({ title: "Source", open: true });

    expect((document.querySelector("details.a-fold") as HTMLDetailsElement).open).toBe(true);
  });
});
