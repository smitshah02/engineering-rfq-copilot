import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import RfqNew from "./RfqNew";
import { json, mockFetch, renderWithProviders, signedIn } from "../test/utils";

const key = (init: RequestInit) => (init.headers as Record<string, string>)["Idempotency-Key"];

describe("RFQ submission Idempotency-Key", () => {
  it("reuses the key for an unchanged retry and uses a new one after an edit", async () => {
    signedIn();
    const answers = [json(503, { detail: "x" }), json(503, { detail: "x" }), json(201, { rfq_id: "RFQ-9", status: "draft" })];
    const fetch = mockFetch(() => answers.shift()!);
    renderWithProviders(
      <Routes>
        <Route path="/rfqs/new" element={<RfqNew />} />
        <Route path="/rfqs/:rfqId" element={<p>detail page</p>} />
      </Routes>,
      "/rfqs/new",
    );
    await userEvent.type(screen.getByLabelText(/Part ID/), "P-1");
    await userEvent.click(screen.getByRole("button", { name: "Submit RFQ" }));
    await userEvent.click(await screen.findByRole("button", { name: "Retry submission" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    await screen.findByRole("button", { name: "Retry submission" });
    await userEvent.type(screen.getByLabelText(/Part ID/), "x");
    await userEvent.click(screen.getByRole("button", { name: "Retry submission" }));
    expect(await screen.findByText("detail page")).toBeInTheDocument();

    const keys = fetch.mock.calls.map(([, init]) => key(init));
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
    expect(keys[2]).not.toBe(keys[0]);
  });
});
