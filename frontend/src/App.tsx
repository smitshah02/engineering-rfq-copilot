import { useState, type FormEvent } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import Dashboard from "./pages/Dashboard";
import RfqList from "./pages/RfqList";
import RfqNew from "./pages/RfqNew";
import RfqDetail from "./pages/RfqDetail";
import CustomerQuoteDetail from "./pages/CustomerQuoteDetail";
import StaffQuoteList from "./pages/StaffQuoteList";
import StaffQuoteDetail from "./pages/StaffQuoteDetail";
import { StaffQuoteAccess, useStaffAccess } from "./staffQuotes";

const NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/rfqs", label: "RFQs", end: true },
  { to: "/rfqs/new", label: "New RFQ", end: false },
];

export default function App() {
  const { token } = useAuth();
  return token ? <Shell /> : <DevTokenGate />;
}

function Shell() {
  const { signOut } = useAuth();
  const staff = useStaffAccess();
  const [open, setOpen] = useState(false);
  const link = ({ isActive }: { isActive: boolean }) =>
    `block rounded-md px-3 py-2 text-sm font-medium ${isActive ? "bg-indigo-50 text-indigo-700" : "text-slate-600 hover:bg-slate-100"}`;
  return (
    <div className="min-h-screen md:flex">
      <aside className={`${open ? "block" : "hidden"} border-b border-slate-200 bg-white md:block md:w-60 md:shrink-0 md:border-b-0 md:border-r`}>
        <div className="hidden h-16 items-center px-5 text-base font-semibold md:flex">RFQ Copilot</div>
        <nav aria-label="Main" className="space-y-1 p-3">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end} className={link} onClick={() => setOpen(false)}>{n.label}</NavLink>
          ))}
          {staff.isSuccess && <NavLink to="/staff/quotes" className={link} onClick={() => setOpen(false)}>Staff quotations</NavLink>}
        </nav>
      </aside>
      <div className="min-w-0 flex-1">
        <header className="flex h-16 items-center justify-between border-b border-slate-200 bg-white px-4 md:px-8">
          <div className="flex items-center gap-3">
            <button className="btn-secondary px-2 md:hidden" aria-label="Toggle navigation" aria-expanded={open} onClick={() => setOpen(!open)}>☰</button>
            <span className="font-semibold md:hidden">RFQ Copilot</span>
            <span className="hidden text-sm text-slate-500 md:inline">{staff.isSuccess ? "Customer and staff portal" : "Customer portal"}</span>
          </div>
          <div className="flex items-center gap-3">
            <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800">Dev token</span>
            <button className="btn-secondary" onClick={signOut}>Sign out</button>
          </div>
        </header>
        <main className="mx-auto max-w-6xl p-4 md:p-8">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/rfqs" element={<RfqList />} />
            <Route path="/rfqs/new" element={<RfqNew />} />
            <Route path="/rfqs/:rfqId" element={<RfqDetail />} />
            <Route path="/quotes/:quoteId" element={<CustomerQuoteDetail />} />
            <Route element={<StaffQuoteAccess />}>
              <Route path="/staff/quotes" element={<StaffQuoteList />} />
              <Route path="/staff/quotes/:quoteId" element={<StaffQuoteDetail />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}

function DevTokenGate() {
  const { signIn } = useAuth();
  const [value, setValue] = useState("");
  const submit = (e: FormEvent) => { e.preventDefault(); if (value.trim()) signIn(value); };
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <form onSubmit={submit} className="card w-full max-w-md space-y-4 p-6">
        <div>
          <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-amber-800">Development only</span>
          <h1 className="mt-3 text-xl font-semibold">RFQ Copilot customer portal</h1>
          <p className="mt-1 text-sm text-slate-500">
            Paste a short-lived development JWT from <code className="font-mono text-xs">python -m rfq_copilot.dev_token issue</code>.
            {import.meta.env.DEV
              ? "It is kept for this browser tab only until it expires or you sign out."
              : "It stays in memory only; reloading the page signs you out."}
          </p>
        </div>
        <div>
          <label htmlFor="dev-token" className="mb-1 block text-sm font-medium">Development token</label>
          <textarea id="dev-token" className="input font-mono text-xs" rows={4} autoComplete="off" spellCheck={false}
            value={value} onChange={(e) => setValue(e.target.value)} />
        </div>
        <button type="submit" className="btn-primary w-full" disabled={!value.trim()}>Continue</button>
      </form>
    </div>
  );
}
