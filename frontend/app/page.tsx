"use client";
import useSWR from "swr";
import Link from "next/link";
import { fetcher, getToken } from "@/lib/api";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function Dashboard() {
  const router = useRouter();
  useEffect(() => { if (!getToken()) router.push("/login"); }, [router]);

  const { data: sources } = useSWR("/api/sources", fetcher);
  const { data: pending } = useSWR("/api/posts?status=drafted", fetcher);
  const { data: published } = useSWR("/api/posts?status=published", fetcher);
  const { data: platforms } = useSWR("/api/meta/platforms", fetcher);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Dashboard</h1>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Stat label="Sources tracked" value={sources?.length ?? "—"} href="/sources" />
        <Stat label="Awaiting validation" value={pending?.length ?? "—"} href="/queue" />
        <Stat label="Published" value={published?.length ?? "—"} href="/posts" />
      </div>

      <section className="card p-4">
        <h2 className="font-semibold mb-3">Connected platforms</h2>
        <div className="flex flex-wrap gap-2">
          {(platforms?.platforms ?? []).map((p: string) => {
            const ok = platforms?.configured?.[p];
            return (
              <span
                key={p}
                className={`badge ${ok ? "bg-emerald-100 text-emerald-800" : "bg-slate-100 text-slate-500"}`}
                title={ok ? "Credentials present" : "Add credentials in backend .env"}
              >
                {p} {ok ? "✓" : "○"}
              </span>
            );
          })}
        </div>
      </section>

      <section className="card p-4">
        <h2 className="font-semibold mb-2">How it works</h2>
        <ol className="list-decimal list-inside text-sm space-y-1 text-slate-700">
          <li>Add sites you want to track on the <Link className="text-brand-600" href="/sources">Sources</Link> page.</li>
          <li>Each day the scheduler scrapes new items and asks the AI to structure them.</li>
          <li>Open the <Link className="text-brand-600" href="/queue">Validation Queue</Link> — review, edit, approve.</li>
          <li>Choose the platforms and publish.</li>
        </ol>
      </section>
    </div>
  );
}

function Stat({ label, value, href }: { label: string; value: any; href: string }) {
  return (
    <Link href={href} className="card p-4 hover:shadow-md transition">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="text-3xl font-bold mt-1">{value}</div>
    </Link>
  );
}
