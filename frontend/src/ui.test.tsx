import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusBadge, statusLabel } from "./ui";

describe("customer RFQ status", () => {
  it.each([
    ["draft", "Received"], ["intake", "Under review"], ["validated", "Under review"],
    ["blocked", "Unable to proceed"], ["closed", "Closed"], ["archived", "Archived"],
  ])("%s is shown as %s", (status, label) => {
    expect(statusLabel(status)).toBe(label);
    render(<StatusBadge status={status} />);
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.queryByText(status)).toBeNull();
  });

  it("never shows an unknown raw status", () => {
    render(<StatusBadge status="pending_internal_xyz" />);
    expect(screen.getByText("Status unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/pending_internal_xyz/)).toBeNull();
  });
});
