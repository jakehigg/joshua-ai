import { Route, Routes } from "react-router-dom";

import Me from "./pages/Me";

/**
 * The routes of the app. Only a page that is migrated is here: every other
 * address of the viewer is still a page of the server renderer, so there is
 * no catch-all route that would swallow it.
 */
export default function App() {
  return (
    <Routes>
      <Route path="/me" element={<Me />} />
    </Routes>
  );
}
