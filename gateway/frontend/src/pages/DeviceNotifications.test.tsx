/**
 * "Notifications on this device", the card of the Profile page: the state it
 * shows, the tap that asks and subscribes, the test send, and the way off
 * again.
 *
 * `push.ts` is the browser half and has its own test; here it is a fake, so
 * the card is tested on what it does with each answer.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { DeviceNotifications } from "./ProfileSettings";
import type { PushStatus } from "../push";

const push = vi.hoisted(() => ({
  readStatus: vi.fn(),
  enable: vi.fn(),
  forget: vi.fn(),
  sendTest: vi.fn(),
}));

vi.mock("../push", () => push);

const OFF: PushStatus = { support: "ready", permission: "default", subscribed: false };
const ON: PushStatus = { support: "ready", permission: "granted", subscribed: true };

function given(status: PushStatus): void {
  push.readStatus.mockReset().mockResolvedValue(status);
  push.enable.mockReset();
  push.forget.mockReset();
  push.sendTest.mockReset();
}

describe("the notifications of this device", () => {
  it("asks for the permission on a tap, and says the device is on", async () => {
    given(OFF);
    push.enable.mockResolvedValue(ON);

    render(<DeviceNotifications />);

    fireEvent.click(await screen.findByRole("button", { name: "Turn on notifications" }));

    expect(await screen.findByText("Notifications are on for this device.")).toBeDefined();
    expect(push.enable).toHaveBeenCalledTimes(1);
  });

  it("says that only the device can allow them again when it refuses", async () => {
    given(OFF);
    push.enable.mockResolvedValue({ ...OFF, permission: "denied" });

    render(<DeviceNotifications />);

    fireEvent.click(await screen.findByRole("button", { name: "Turn on notifications" }));

    expect(await screen.findByText(/settings of the device/)).toBeDefined();
    expect(screen.queryByRole("button", { name: "Turn on notifications" })).toBeNull();
  });

  it("asks nothing of a device that blocks them", async () => {
    given({ support: "ready", permission: "denied", subscribed: false });

    render(<DeviceNotifications />);

    expect(await screen.findByText(/settings of the device/)).toBeDefined();
    expect(screen.queryByRole("button")).toBeNull();
    expect(push.enable).not.toHaveBeenCalled();
  });

  it("asks for the app on the home screen on an iPhone", async () => {
    given({ support: "install", permission: "default", subscribed: false });

    render(<DeviceNotifications />);

    expect(await screen.findByText(/add Joshua to the home screen/)).toBeDefined();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("says that the browser cannot show notifications at all", async () => {
    given({ support: "unsupported", permission: "default", subscribed: false });

    render(<DeviceNotifications />);

    expect(await screen.findByText("This browser cannot show notifications.")).toBeDefined();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("offers the test send, and says when no device took it", async () => {
    given(ON);
    push.sendTest.mockResolvedValueOnce({ sent: 1, failed: 0, dropped: 0, held: 0, error: null });
    push.sendTest.mockResolvedValueOnce({ sent: 0, failed: 1, dropped: 0, held: 0, error: null });

    render(<DeviceNotifications />);

    fireEvent.click(await screen.findByRole("button", { name: "Send a test" }));
    expect(await screen.findByText("A test notification is on its way.")).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: "Send a test" }));
    expect(await screen.findByText("No device took the test notification.")).toBeDefined();
  });

  it("drops the subscription when the person turns it off", async () => {
    given(ON);
    push.forget.mockResolvedValue(OFF);

    render(<DeviceNotifications />);

    fireEvent.click(await screen.findByRole("button", { name: "Turn off" }));

    expect(await screen.findByText("This device gets no notifications yet.")).toBeDefined();
    expect(push.forget).toHaveBeenCalledTimes(1);
  });

  it("shows what the server said when push is off or a send cannot be built", async () => {
    given(OFF);
    push.enable.mockRejectedValue(new ApiError(503, "notifications are not configured"));

    render(<DeviceNotifications />);

    fireEvent.click(await screen.findByRole("button", { name: "Turn on notifications" }));

    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      "notifications are not configured",
    );
  });

  it("shows the real message of a test send the server could not build", async () => {
    given(ON);
    push.sendTest.mockRejectedValue(
      new ApiError(400, "VapidException: Missing 'sub' from claims."),
    );

    render(<DeviceNotifications />);

    fireEvent.click(await screen.findByRole("button", { name: "Send a test" }));

    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      "VapidException: Missing 'sub' from claims.",
    );
  });
});
