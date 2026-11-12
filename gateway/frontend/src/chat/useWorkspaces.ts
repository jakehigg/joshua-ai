// The workspaces of the person, for the rail. Loads on open and on reload().

import { useCallback, useEffect, useState } from "react";
import { listWorkspaces, type Workspace } from "../api/workspaces";

export interface Workspaces {
  list: Workspace[];
  reload: () => void;
}

export function useWorkspaces(): Workspaces {
  const [list, setList] = useState<Workspace[]>([]);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    listWorkspaces()
      .then((rows) => {
        if (live) setList(rows);
      })
      .catch(() => undefined); // The rail shows what it has; the chat works without it.
    return () => {
      live = false;
    };
  }, [tick]);
  const reload = useCallback(() => setTick((n) => n + 1), []);
  return { list, reload };
}
