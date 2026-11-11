import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Each test gets a clean document.
afterEach(() => {
  cleanup();
});
