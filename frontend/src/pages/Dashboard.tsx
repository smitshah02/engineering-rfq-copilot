import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import type { CustomerProfile, RfqPage } from "../api";
import { useApi } from "../auth";
import { ErrorPanel, Loading, PageHeader, RfqTable } from "../ui";

export default function Dashboard() {
  const api = useApi();
  const profile = useQuery({ queryKey: ["me"], queryFn: async () => (await api<CustomerProfile>("/customers/me")).data });
  const recent = useQuery({ queryKey: ["rfqs", 0, 5], queryFn: async () => (await api<RfqPage>("/rfqs?limit=5&offset=0")).data });

  return (
    <>
      <PageHeader
        title={profile.data ? profile.data.company_name : "Dashboard"}
        subtitle="Your account and most recent requests for quotation."
        action={<Link to="/rfqs/new" className="btn-primary">New RFQ</Link>}
      />
      <div className="grid gap-6 lg:grid-cols-3">
        <section className="card p-5 lg:col-span-1" aria-labelledby="account-h">
          <h2 id="account-h" className="text-sm font-semibold text-slate-700">Account</h2>
          {profile.isPending ? <Loading /> : profile.isError ? <div className="mt-3"><ErrorPanel error={profile.error} onRetry={() => profile.refetch()} /></div> : (
            <dl className="mt-3 space-y-2 text-sm">
              {([
                ["Customer ID", profile.data.customer_id],
                ["Industry", profile.data.industry],
                ["Location", [profile.data.city, profile.data.state, profile.data.country].filter(Boolean).join(", ")],
                ["Payment terms", profile.data.payment_terms],
                ["Shipping", profile.data.preferred_shipping],
              ] as const).map(([k, v]) => (
                <div key={k} className="flex justify-between gap-4"><dt className="text-slate-500">{k}</dt><dd className="text-right font-medium">{v}</dd></div>
              ))}
              {profile.data.contacts.filter((c) => c.is_primary).map((c) => (
                <div key={c.email} className="flex justify-between gap-4"><dt className="text-slate-500">Primary contact</dt><dd className="text-right font-medium">{c.contact_name}</dd></div>
              ))}
            </dl>
          )}
        </section>
        <section className="card lg:col-span-2" aria-labelledby="recent-h">
          <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
            <h2 id="recent-h" className="text-sm font-semibold text-slate-700">Recent RFQs</h2>
            <Link to="/rfqs" className="text-sm text-indigo-600 hover:underline">View all</Link>
          </div>
          {recent.isPending ? <Loading /> : recent.isError ? <div className="p-4"><ErrorPanel error={recent.error} onRetry={() => recent.refetch()} /></div> : <RfqTable items={recent.data.items} />}
        </section>
      </div>
    </>
  );
}
