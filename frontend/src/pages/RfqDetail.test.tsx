import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import RfqDetail from "./RfqDetail";
import { json, mockFetch, renderWithProviders, signedIn } from "../test/utils";

const RFQ = "RFQ-1";
const DETAIL = {
  rfq_id: RFQ, status: "draft", submitted_at: "2026-11-18T10:00:00Z",
  lines: [{ line_number: 1, part: { part_id: "P-1", quantity: 5 } }],
};
const ATTACHMENT = { attachment_id: "ATT-abc", rfq_id: RFQ, filename: "drawing.pdf", document_type: "pdf", revision: null, sha256: "a".repeat(64) };

/** Backend fixture: the detail, an attachment list that grows on upload, and a POST answer. */
function backend(post: () => Response = () => json(201, { ...ATTACHMENT, size_bytes: 8, extraction: { kind: "pdf", status: "text", page_count: 1, pages: [], truncated: false } })) {
  const items: unknown[] = [];
  let listCalls = 0;
  const fetch = mockFetch((url, init) => {
    if (url === `/api/v1/rfqs/${RFQ}`) return json(200, DETAIL);
    if (url === `/api/v1/rfqs/${RFQ}/attachments` && init.method === "POST") {
      const res = post();
      if (res.ok) items.push(ATTACHMENT);
      return res;
    }
    if (url === `/api/v1/rfqs/${RFQ}/attachments`) { listCalls++; return json(200, { items: [...items] }); }
    return json(404, {});
  });
  return { fetch, listCalls: () => listCalls };
}

const show = (state: object | null = null) => {
  signedIn();
  return renderWithProviders(
    <Routes><Route path="/rfqs/:rfqId" element={<RfqDetail />} /></Routes>,
    { pathname: `/rfqs/${RFQ}`, state },
  );
};

const fileInput = () => screen.getByLabelText(/Add a drawing/) as HTMLInputElement;
const pdf = (name = "drawing.pdf", content = "%PDF-1.4") => new File([content], name, { type: "application/pdf" });

describe("RFQ detail: status and confirmation", () => {
  it("shows the corrected confirmation after a new submission", async () => {
    backend();
    show({ submitted: true });
    expect(await screen.findByText(
      "Your RFQ was received successfully. No additional submission is required. A quotation will be shared only after authorized review, approval, and release.",
    )).toBeInTheDocument();
  });

  it("shows a concise duplicate-submission confirmation", async () => {
    backend();
    show({ submitted: true, replayed: true });
    expect(await screen.findByText(/already received/)).toBeInTheDocument();
  });

  it("shows the customer label and no evaluation, agent or draft information", async () => {
    backend();
    show({ submitted: true });
    expect(await screen.findAllByText("Received")).not.toHaveLength(0);
    expect(screen.queryByText("draft")).toBeNull();
    expect(screen.queryByText(/evaluat|agent|supervisor|workflow|pricing|draft quot|processing has started/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /evaluate/i })).toBeNull();
  });
});

describe("RFQ detail: attachments", () => {
  it("lists nothing, uploads a PDF in one 'file' field and refetches the list", async () => {
    const { fetch, listCalls } = backend();
    show();
    expect(await screen.findByText("No attachments yet.")).toBeInTheDocument();
    const before = listCalls();
    await userEvent.upload(fileInput(), pdf());
    await userEvent.click(screen.getByRole("button", { name: "Upload" }));
    expect(await screen.findByText("Uploaded drawing.pdf.")).toBeInTheDocument();
    expect(await screen.findByText("drawing.pdf")).toBeInTheDocument();
    expect(listCalls()).toBeGreaterThan(before);
    const post = fetch.mock.calls.find(([, init]) => init.method === "POST")!;
    expect([...(post[1].body as FormData).keys()]).toEqual(["file"]);
  });

  it("accepts a CSV file", async () => {
    backend();
    show();
    await screen.findByText("No attachments yet.");
    await userEvent.upload(fileInput(), new File(["rfq_id\n"], "bom.csv", { type: "text/csv" }));
    expect(screen.getByText("One file at a time, up to 10 MiB.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload" })).toBeEnabled();
  });

  it("rejects an unsupported type, an empty file and an oversized file before sending", async () => {
    const { fetch } = backend();
    show();
    await screen.findByText("No attachments yet.");
    const user = userEvent.setup({ applyAccept: false });
    const upload = screen.getByRole("button", { name: "Upload" });

    await user.upload(fileInput(), new File(["x"], "photo.png", { type: "image/png" }));
    expect(screen.getByText("Only PDF and CSV files are accepted.")).toBeInTheDocument();
    expect(upload).toBeDisabled();

    await user.upload(fileInput(), pdf("empty.pdf", ""));
    expect(screen.getByText("The file is empty.")).toBeInTheDocument();
    expect(upload).toBeDisabled();

    const big = pdf("big.pdf");
    Object.defineProperty(big, "size", { value: 10 * 1024 * 1024 + 1 });
    await user.upload(fileInput(), big);
    expect(screen.getByText("The file is larger than 10 MiB.")).toBeInTheDocument();
    expect(upload).toBeDisabled();

    expect(fetch.mock.calls.some(([, init]) => init.method === "POST")).toBe(false);
  });

  it("shows the backend rejection message and its CSV row issues", async () => {
    backend(() => json(422, {
      code: "ATTACHMENT_REJECTED", message: "CSV rows are missing or invalid.", reason: "bom_rows",
      issues: [{ row: 2, column: "quantity", code: "invalid_integer" }],
    }));
    show();
    await screen.findByText("No attachments yet.");
    await userEvent.upload(fileInput(), new File(["x"], "bom.csv", { type: "text/csv" }));
    await userEvent.click(screen.getByRole("button", { name: "Upload" }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("CSV rows are missing or invalid.")).toBeInTheDocument();
    expect(within(alert).getByText("Row 2, quantity: invalid whole number")).toBeInTheDocument();
  });

  it("shows fixed text, not internal detail, for an unstructured server error", async () => {
    backend(() => json(503, { detail: "storage path /secret failed" }));
    show();
    await screen.findByText("No attachments yet.");
    await userEvent.upload(fileInput(), pdf());
    await userEvent.click(screen.getByRole("button", { name: "Upload" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The service is temporarily unavailable. Try again shortly.");
    expect(alert).not.toHaveTextContent("/secret");
  });
});
