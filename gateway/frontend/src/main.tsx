import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import { registerServiceWorker } from "./pwa";
import { startTextScale } from "./textSize";
import "./index.css";

// Before the first render, so a reload paints at the size of this device
// right away and never flashes the normal one first.
startTextScale();

const root = document.getElementById("root");
if (root === null) {
  throw new Error("the page has no root element");
}

createRoot(root).render(
  <StrictMode>
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
    </BrowserRouter>
  </StrictMode>,
);

registerServiceWorker();
