/** What a list with nothing in it shows. */
export default function Empty({ text = "nothing yet." }: { text?: string }) {
  return <p className="j-empty">{text}</p>;
}
