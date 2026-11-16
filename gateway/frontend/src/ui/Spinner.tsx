/**
 * The wait mark of a page that reads its data: one larger spinner, centred
 * both ways in the content area the page gives it.
 *
 * It is the one spinner of every full-page loading state, so its size and its
 * place change in one file. A spinner inside a button or a row is not this
 * component: it is the bare `.j-spinner` class, which keeps its small size.
 */
export default function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div className="j-loading">
      <div className="j-spinner lg" role="status" aria-label={label} />
    </div>
  );
}
