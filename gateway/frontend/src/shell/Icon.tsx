/**
 * One icon of the navigation, from the path data of the section table.
 *
 * The path data is a constant of the app (`sections.ts`), never a value from
 * a person and never a value from the server, so it goes into the element as
 * it is.
 */
export default function Icon({ paths }: { paths: string }) {
  return (
    <svg
      className="j-icon"
      viewBox="0 0 24 24"
      aria-hidden="true"
      dangerouslySetInnerHTML={{ __html: paths }}
    />
  );
}
