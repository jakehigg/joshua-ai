import { useContext, useEffect, useState, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";

import { ApiError, getJson } from "./api/client";
import Admin from "./pages/Admin";
import Assignment from "./pages/Assignment";
import { ChatPage, WorkspaceLink, WorkspacePage } from "./chat/ChatPage";
import { ChatHome } from "./chat/Home";
import Assignments from "./pages/Assignments";
import Document from "./pages/Document";
import Documents from "./pages/Documents";
import Edit from "./pages/Edit";
import FileRedirect, { type WikiFileData } from "./pages/FileRedirect";
import Journal from "./pages/Journal";
import JournalDay from "./pages/JournalDay";
import Links from "./pages/Links";
import Me from "./pages/Me";
import New from "./pages/New";
import Saved from "./pages/Saved";
import SavedAssignment from "./pages/SavedAssignment";
import Search from "./pages/Search";
import SignIn, { type AuthMethod } from "./pages/SignIn";
import Usage from "./pages/Usage";
import WikiFolder, { type WikiFolderData } from "./pages/WikiFolder";
import WikiPage, { type WikiPageData } from "./pages/WikiPage";
import Shell, { SessionContext, type Session } from "./shell/Shell";
import ErrorLine from "./ui/ErrorLine";
import Spinner from "./ui/Spinner";

/** The answer of `GET /api/v1/wiki`, by what the path names. */
type Answer = WikiPageData | WikiFolderData | WikiFileData;

/**
 * One address of the content: the page, the folder, or the file at it.
 *
 * The splat of the route is the path under the root, so the address is the
 * root-relative path the JSON route reads. A trailing slash and a path that
 * names a directory both answer a folder, because the route answers by what
 * the path is.
 */
function Content({ root }: { root: string }) {
  const splat = useParams()["*"] ?? "";
  const path = `${root}/${splat}`;
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let live = true;
    setAnswer(null);
    setError("");
    getJson<Answer>(`/wiki?path=${encodeURIComponent(path)}`)
      .then((body) => {
        if (live) {
          setAnswer(body);
        }
      })
      .catch((exc: unknown) => {
        if (live) {
          setError(exc instanceof ApiError ? exc.message : "the page did not load");
        }
      });
    return () => {
      live = false;
    };
  }, [path]);

  if (error !== "") {
    return (
      <Shell section={root === "wiki" ? "wiki" : ""}>
        <ErrorLine text={error} />
      </Shell>
    );
  }
  if (answer === null) {
    return (
      <Shell section={root === "wiki" ? "wiki" : ""}>
        <Spinner label="Loading the page" />
      </Shell>
    );
  }
  if (answer.kind === "page") {
    return <WikiPage page={answer} />;
  }
  if (answer.kind === "folder") {
    return <WikiFolder folder={answer} />;
  }
  return <FileRedirect file={answer} />;
}

/** The address of the sign-in page, the one page that needs no session. */
const SIGN_IN = "/sign-in";

/** What the app knows about the session of this browser while it reads it. */
type Gate = { at: "waiting" } | { at: "app"; session: Session | null } | { at: "sign-in" };

/**
 * The session of this browser, for every page under it.
 *
 * `GET /api/v1/session` is the one call that says who is signed in, so the
 * app makes it once and the shell reads the answer from the context instead
 * of asking again. A 401 means that nobody is signed in: the app goes to the
 * sign-in page, rather than letting each page draw the refusal. On an
 * anonymous instance that 401 cannot happen, because every request is served
 * as the anonymous identity, so a refusal there is other trouble and each
 * page shows its own error as before.
 *
 * The sign-in page itself is not gated: it is what a person without a
 * session is sent to.
 */
function SignedIn({ children }: { children: ReactNode }) {
  const atSignIn = useLocation().pathname === SIGN_IN;
  const [gate, setGate] = useState<Gate>({ at: "waiting" });

  useEffect(() => {
    if (atSignIn) {
      // Forget the verdict of the last visit. A sign-in changes the answer,
      // and a stale "sign-in" here would send the browser straight back to
      // the form on the first render after it.
      setGate({ at: "waiting" });
      return undefined;
    }
    let live = true;
    const settle = (next: Gate) => {
      if (live) {
        setGate(next);
      }
    };
    getJson<Session>("/session")
      .then((answer) => settle({ at: "app", session: answer }))
      .catch(async (exc: unknown) => {
        if (!(exc instanceof ApiError) || exc.status !== 401) {
          settle({ at: "app", session: null });
          return;
        }
        const auth = await getJson<AuthMethod>("/auth-method").catch(() => null);
        settle(auth?.method === "anonymous" ? { at: "app", session: null } : { at: "sign-in" });
      });
    return () => {
      live = false;
    };
  }, [atSignIn]);

  if (atSignIn) {
    return <>{children}</>;
  }
  if (gate.at === "waiting") {
    return <Spinner label="Loading Joshua" />;
  }
  if (gate.at === "sign-in") {
    return <Navigate to={SIGN_IN} replace />;
  }
  return <SessionContext.Provider value={gate.session}>{children}</SessionContext.Provider>;
}

/** The one role that may see the admin area. */
const OWNER_ROLE = "owner";

/**
 * The admin area, for the owner and for nobody else.
 *
 * The role comes from the session the app already read. Another person gets
 * what an address that no route claims gets — nothing — because the viewer
 * does not tell a member that there is a page they may not see. The routes
 * behind the page refuse them as well, so this is the sight of it and not
 * the guard of it.
 */
function OwnerOnly({ children }: { children: ReactNode }) {
  const session = useContext(SessionContext);
  if (session?.person.role !== OWNER_ROLE) {
    return null;
  }
  return <>{children}</>;
}

/**
 * The routes of the app. Every screen of the viewer is here: there is no
 * other renderer. An address that no route claims renders an empty page,
 * because the server has no page of its own behind this app.
 */
export default function App() {
  return (
    <SignedIn>
      <Routes>
        <Route path={SIGN_IN} element={<SignIn />} />
        <Route path="/" element={<Navigate to="/wiki/" replace />} />
        <Route path="/wiki/*" element={<Content root="wiki" />} />
        <Route path="/shared/*" element={<Content root="shared" />} />
        <Route path="/attachments/*" element={<Content root="attachments" />} />
        <Route path="/assignments" element={<Assignments />} />
        <Route path="/assignments/saved" element={<Saved />} />
        <Route path="/assignments/saved/:id" element={<SavedAssignment />} />
        <Route path="/assignments/:id" element={<Assignment />} />
        <Route path="/documents" element={<Documents />} />
        <Route path="/documents/:owner/:slug" element={<Document />} />
        <Route path="/journal" element={<Journal />} />
        <Route path="/journal/:year/:month/:day" element={<JournalDay />} />
        <Route path="/search" element={<Search />} />
        <Route path="/usage" element={<Usage />} />
        <Route path="/links" element={<Links />} />
        <Route path="/edit/*" element={<Edit />} />
        <Route path="/new" element={<New />} />
        <Route path="/chat" element={<ChatHome />} />
        <Route path="/chat/w/:space/:slug" element={<WorkspacePage />} />
        <Route path="/chat/w/:ref" element={<WorkspaceLink />} />
        <Route path="/chat/:person" element={<ChatPage />} />
        <Route path="/me" element={<Me />} />
        <Route
          path="/admin"
          element={
            <OwnerOnly>
              <Admin />
            </OwnerOnly>
          }
        />
        <Route
          path="/admin/users"
          element={
            <OwnerOnly>
              <Admin />
            </OwnerOnly>
          }
        />
      </Routes>
    </SignedIn>
  );
}
