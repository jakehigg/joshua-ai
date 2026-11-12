// Person ids. The backend (viewer_api/chat.py) keeps the same pattern.
export const PERSON_ID = /^[a-z][a-z0-9-]{0,31}$/;

export function validPerson(person: string): boolean {
  return PERSON_ID.test(person);
}
