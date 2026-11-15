// The notifications of this device, in the edit drawer of a workspace, under
// "Messages to your phone": that setting says when Joshua writes, and this
// says whether this device hears it.
//
// One tap asks for the permission and subscribes (`push.ts`), because the
// permission prompt needs the gesture of a person. The control then says what
// the state is, and offers the test send and a way off again:
//
// - "install": iOS, in a tab. Notifications arrive only in the app on the
//   home screen, so that is what it asks for.
// - "denied": the browser refused, and only the settings of the device can
//   undo it. The control says so and asks nothing.
// - on: this device is subscribed. "Send a test" is the one way to see a
//   notification arrive, and "Turn off" drops the subscription here and on
//   the server.
//
// While the owner has not turned push on, `GET /api/v1/push/key` answers 503
// with the sentence that says so, and the control shows that sentence.

import { useEffect, useState } from "react";

import { errorText } from "../api/client";
import { enable, forget, readStatus, sendTest, type PushStatus } from "../push";

const ON = "Notifications are on for this device.";
const OFF = "This device gets no notifications yet.";
const DENIED =
  "This device blocks notifications. Allow them for Joshua in the settings of the device, then come back.";
const INSTALL =
  "On an iPhone or an iPad, add Joshua to the home screen and open it from there. Notifications arrive in the app only.";
const UNSUPPORTED = "This browser cannot show notifications.";
const TEST_SENT = "A test notification is on its way.";
const TEST_NONE = "No device took the test notification.";

export function Notifications() {
  const [status, setStatus] = useState<PushStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    let live = true;
    readStatus()
      .then((found) => live && setStatus(found))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  // One step: it settles the status, says what happened, and never leaves the
  // control busy.
  const run = (step: () => Promise<PushStatus | string>, fallback: string) => {
    setBusy(true);
    setError("");
    setNote("");
    step()
      .then((answer) => {
        if (typeof answer === "string") setNote(answer);
        else setStatus(answer);
      })
      .catch((e: unknown) => setError(errorText(e, fallback)))
      .finally(() => setBusy(false));
  };

  // A refusal is not an error: `enable` answers the status that says so, and
  // the sentence below becomes the one of a device that blocks them.
  const turnOn = () => run(() => enable(), "notifications could not be turned on");

  const turnOff = () => run(() => forget(), "the subscription was not dropped");

  const test = () =>
    run(async () => {
      const report = await sendTest();
      return report.sent > 0 ? TEST_SENT : (report.error ?? TEST_NONE);
    }, "the test notification was not sent");

  if (status === null) {
    return null;
  }

  const sentence =
    status.support === "install"
      ? INSTALL
      : status.support === "unsupported"
        ? UNSUPPORTED
        : status.permission === "denied"
          ? DENIED
          : status.subscribed
            ? ON
            : OFF;

  return (
    <section className="ws-push" aria-label="Notifications">
      <span className="j-field-label">Notifications</span>
      <p className="ws-push-state">{sentence}</p>
      {status.support === "ready" && status.permission !== "denied" && (
        <div className="ws-push-actions">
          {status.subscribed ? (
            <>
              <button type="button" disabled={busy} onClick={test}>
                Send a test
              </button>
              <button type="button" disabled={busy} onClick={turnOff}>
                Turn off
              </button>
            </>
          ) : (
            <button type="button" disabled={busy} onClick={turnOn}>
              Turn on notifications
            </button>
          )}
        </div>
      )}
      {note && <p className="ws-push-note">{note}</p>}
      {error && (
        <p className="j-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
