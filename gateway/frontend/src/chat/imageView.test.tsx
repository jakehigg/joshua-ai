import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PagePanel } from "./DocumentPanel";

describe("the image view of the panel", () => {
  it("shows the photo full size, with a link to open it, and closes", () => {
    const onClose = vi.fn();
    render(<PagePanel view={{ type: "image", image: { url: "/api/v1/files/wiki/a.jpg", title: "Display" } }} onClose={onClose} />);
    expect(screen.getByAltText("Display").getAttribute("src")).toBe("/api/v1/files/wiki/a.jpg");
    expect(screen.getByRole("link", { name: "Open in new tab" }).getAttribute("href")).toBe("/api/v1/files/wiki/a.jpg");
    fireEvent.click(screen.getByRole("button", { name: "Close the item" }));
    expect(onClose).toHaveBeenCalled();
  });
});
