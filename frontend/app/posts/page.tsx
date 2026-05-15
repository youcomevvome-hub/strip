"use client";
import useSWR from "swr";
import { fetcher } from "@/lib/api";

export default function PostsPage() {
  const { data } = useSWR("/api/posts", fetcher);
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Posts</h1>
      <div className="space-y-3">
        {(data ?? []).map((p: any) => (
          <div key={p.id} className="card p-4">
            <div className="flex justify-between items-start">
              <div className="flex-1">
                <div className="font-semibold">{p.title}</div>
                <div className="text-sm text-slate-600 mt-1">{p.summary}</div>
              </div>
              <span className={`badge ${
                p.status === "published" ? "bg-emerald-100 text-emerald-800"
                  : p.status === "rejected" ? "bg-red-100 text-red-700"
                  : p.status === "approved" ? "bg-blue-100 text-blue-700"
                  : "bg-amber-100 text-amber-800"
              }`}>{p.status}</span>
            </div>
            {p.deliveries?.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-2">
                {p.deliveries.map((d: any) => (
                  <span key={d.id} className={`badge ${
                    d.status === "ok" ? "bg-emerald-100 text-emerald-800"
                      : d.status === "skipped" ? "bg-slate-100 text-slate-600"
                      : "bg-red-100 text-red-700"
                  }`} title={d.error ?? ""}>
                    {d.platform}: {d.status}
                    {d.external_url && <> · <a className="underline" href={d.external_url} target="_blank">view</a></>}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
