// The message that is on its way to Joshua, kept in the browser until Joshua
// says it has the message.
//
// A phone can lock, lose its signal, or throw the page away in the middle of
// a send. send() in useTurn.ts saves the text and the file here before the
// request starts, and clears them when the `accepted` frame comes (the turn
// is then Joshua's) or when Joshua refuses the message. A draft that is still
// here on the next open is a send that did not arrive: the chat page offers
// to send it again, file included.
//
// There is one draft for each person and workspace. The store is IndexedDB,
// because a File is kept as it is. Every call is wrapped: a browser that has
// no IndexedDB, or a private window that refuses it, makes each call do
// nothing and the chat works without a draft.

const DB_NAME = "joshua-chat";
const STORE = "drafts";

export interface Draft {
  person: string;
  // The workspace slug, or "" for the main chat.
  workspace: string;
  text: string;
  file: File | null;
  savedAt: number;
}

const keyOf = (person: string, workspace: string) => `${person}:${workspace}`;

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("no IndexedDB"));
      return;
    }
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

// Runs one request of the store. The answer is undefined when anything fails.
async function run<T>(mode: IDBTransactionMode, make: (s: IDBObjectStore) => IDBRequest<T>): Promise<T | undefined> {
  try {
    const db = await open();
    try {
      return await new Promise<T>((resolve, reject) => {
        const tx = db.transaction(STORE, mode);
        const request = make(tx.objectStore(STORE));
        tx.oncomplete = () => resolve(request.result);
        tx.onerror = tx.onabort = () => reject(tx.error);
      });
    } finally {
      db.close();
    }
  } catch {
    return undefined;
  }
}

export async function saveDraft(draft: Draft): Promise<void> {
  await run("readwrite", (s) => s.put(draft, keyOf(draft.person, draft.workspace)));
}

export async function loadDraft(person: string, workspace: string): Promise<Draft | null> {
  const found = await run<Draft | undefined>("readonly", (s) => s.get(keyOf(person, workspace)));
  return found ?? null;
}

export async function clearDraft(person: string, workspace: string): Promise<void> {
  await run("readwrite", (s) => s.delete(keyOf(person, workspace)));
}
