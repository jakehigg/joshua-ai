// Person ids. The backend (viewer_api/chat.py) keeps the same pattern.
export const PERSON_ID = /^[a-z][a-z0-9-]{0,31}$/;

// `w` is reserved: `/chat/w/<slug>` is a workspace, never the chat of a person.
export const RESERVED_PERSON = "w";

export function validPerson(person: string): boolean {
  return PERSON_ID.test(person) && person !== RESERVED_PERSON;
}
