"use client";
import useSWR from "swr";
import { fetcher } from "@/lib/api";

export default function SettingsPage() {
  const { data } = useSWR("/api/meta/platforms", fetcher);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Settings</h1>

      <section className="card p-5">
        <h2 className="font-semibold mb-3">Platform credentials</h2>
        <p className="text-sm text-slate-600 mb-3">
          Configure these in the backend <code>.env</code> file. Restart the backend to apply.
        </p>
        <ul className="text-sm grid grid-cols-1 md:grid-cols-3 gap-2">
          {(data?.platforms ?? []).map((p: string) => {
            const ok = data.configured?.[p];
            return (
              <li key={p} className="flex items-center justify-between border rounded px-3 py-2">
                <span className="capitalize">{p}</span>
                <span className={`badge ${ok ? "bg-emerald-100 text-emerald-800" : "bg-slate-100 text-slate-500"}`}>
                  {ok ? "configured" : "missing"}
                </span>
              </li>
            );
          })}
        </ul>
      </section>

      <section className="card p-5">
        <h2 className="font-semibold mb-3">API for third-party apps</h2>
        <p className="text-sm text-slate-600 mb-2">
          Send <code>X-API-Key: &lt;your key&gt;</code> with requests to integrate from another app or service.
        </p>
        <p className="text-sm">
          OpenAPI docs:&nbsp;
          <a className="text-brand-600 underline"
             href={`${process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000"}/docs`}
             target="_blank">/docs</a>
        </p>
      </section>
    </div>
  );
}
