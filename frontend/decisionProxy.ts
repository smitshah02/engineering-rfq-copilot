// These expressions are mutually exclusive, including the prefix without a suffix.
export const DECISION_PROXY = "^/api/v1/quote-decisions(?:[/?]|$)";
export const ORDINARY_PROXY = "^/api(?!/v1/quote-decisions(?:[/?]|$))";

// Development decisions remain on a separate loopback process. Refuse credentials,
// remote hosts and path rewrites; production hosting must route this prefix explicitly.
export function decisionTarget(value = "http://127.0.0.1:8001") {
  const target = new URL(value);
  if (target.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(target.hostname)
      || target.username || target.password || target.pathname !== "/" || target.search || target.hash)
    throw new Error("RFQ_DECISION_TARGET must be a loopback HTTP origin without credentials or a path.");
  return target.origin;
}
