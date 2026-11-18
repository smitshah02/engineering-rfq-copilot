import { describe, expect, it } from "vitest";
import { ApiError, apiRequest, uploadAttachment, type ApiCall } from "./api";
import { json, mockFetch } from "./test/utils";

const fail = (p: Promise<unknown>) => p.then(() => { throw new Error("expected failure"); }, (e: unknown) => e as ApiError);

describe("apiRequest", () => {
  it("sends the bearer token and returns data", async () => {
    const fetch = mockFetch(() => json(200, { ok: true }));
    const { data } = await apiRequest<{ ok: boolean }>("T", "/x");
    expect(data.ok).toBe(true);
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/x");
    expect((fetch.mock.calls[0][1].headers as Record<string, string>).Authorization).toBe("Bearer T");
  });

  it("uses the structured backend message and keeps reason, details and issues", async () => {
    mockFetch(() => json(422, {
      code: "ATTACHMENT_REJECTED", message: "CSV rows are missing or invalid.", reason: "bom_rows",
      issues: [{ row: 2, column: "quantity", code: "invalid_integer" }],
    }));
    const e = await fail(apiRequest("T", "/x"));
    expect(e).toBeInstanceOf(ApiError);
    expect([e.status, e.message, e.reason]).toEqual([422, "CSV rows are missing or invalid.", "bom_rows"]);
    expect(e.issues).toHaveLength(1);
  });

  it("replaces an unstructured error body with fixed text", async () => {
    mockFetch(() => json(503, { detail: "internal detail" }));
    const e = await fail(apiRequest("T", "/x"));
    expect(e.message).toBe("The service is temporarily unavailable. Try again shortly.");
    expect(e.message).not.toContain("internal");
  });

  it("reports a network failure as status 0", async () => {
    mockFetch(() => { throw new TypeError("offline"); });
    const e = await fail(apiRequest("T", "/x"));
    expect(e.status).toBe(0);
  });
});

describe("uploadAttachment", () => {
  it("posts multipart with one 'file' field and no manual Content-Type", async () => {
    const fetch = mockFetch(() => json(201, { filename: "a.pdf" }));
    const api: ApiCall = (path, init) => apiRequest("T", path, init);
    const file = new File(["%PDF-1.4"], "a.pdf", { type: "application/pdf" });
    await uploadAttachment(api, "RFQ-1", file);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("/api/v1/rfqs/RFQ-1/attachments");
    expect(init.method).toBe("POST");
    const body = init.body as FormData;
    expect([...body.keys()]).toEqual(["file"]);
    expect((body.get("file") as File).name).toBe("a.pdf");
    expect(Object.keys(init.headers as object).map((k) => k.toLowerCase())).not.toContain("content-type");
  });
});
