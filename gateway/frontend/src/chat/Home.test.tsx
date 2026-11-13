import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatHome, LAST_PERSON_KEY, personProblem } from "./Home";

function Where() {
  return <p>at {useLocation().pathname}</p>;
}

function open() {
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <Routes>
        <Route path="/chat" element={<ChatHome />} />
        <Route path="/chat/:person" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 404 })));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ChatHome", () => {
  it("opens the chat of the id at /chat/<id> and remembers it", () => {
    open();
    fireEvent.change(screen.getByLabelText("Your id"), { target: { value: "mia" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));
    expect(screen.getByText("at /chat/mia")).toBeTruthy();
    expect(localStorage.getItem(LAST_PERSON_KEY)).toBe("mia");
  });

  it("fills in the last id, and refuses a bad one", () => {
    localStorage.setItem(LAST_PERSON_KEY, "alex");
    open();
    expect((screen.getByLabelText("Your id") as HTMLInputElement).value).toBe("alex");
    fireEvent.change(screen.getByLabelText("Your id"), { target: { value: "Bad Id" } });
    expect((screen.getByRole("button", { name: "Go" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/Use a lowercase letter first/)).toBeTruthy();
  });
});

describe("personProblem", () => {
  it("says nothing for an empty or a valid id", () => {
    expect(personProblem("")).toBeNull();
    expect(personProblem("alex")).toBeNull();
    expect(personProblem("1x")).not.toBeNull();
  });
});
