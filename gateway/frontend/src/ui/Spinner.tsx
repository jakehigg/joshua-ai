/** The wait mark of a page that reads its data. */
export default function Spinner({ label = "Loading" }: { label?: string }) {
  return <div className="j-spinner" role="status" aria-label={label} />;
}
