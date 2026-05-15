"use client";
import useSWR from "swr";
import { useState } from "react";
import { api, fetcher } from "@/lib/api";

type Source = {
  id: number; name: string; url: string; rss_url?: string | null;
  scrape_mode: string; tags: string; enabled: boolean; last_scraped_at?: string | null;
};

export default function SourcesPage() {
  const { data, mutate } = useSWR<Source[]>("/api/sources", fetcher);
  const [form, setForm] = useState({ name: "", url: "", rss_url: "", scrape_mode: "auto", tags: "" });
  const [busy, setBusy] = useState(false);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/api/sources", {
        method: "POST",
        body: JSON.stringify({
          ...form,
          rss_url: form.rss_url || null,
        }),
      });
      setForm({ name: "", url: "", rss_url: "", scrape_mode: "auto", tags: "" });
      mutate();
    } finally { setBusy(false); }
  }

  async function scrape(id: number) {
    await api(`/api/sources/${id}/scrape`, { method: "POST" });
    alert("Scrape queued. Check the validation queue in a moment.");
  }

  async function scrapeAll() {
    await api(`/api/sources/scrape-all`, { method: "POST" });
    alert("Scrape-all queued.");
  }

  async function del(id: number) {
    if (!confirm("Delete this source?")) return;
    await api(`/api/sources/${id}`, { method: "DELETE" });
    mutate();
  }

  async function toggle(s: Source) {
    await api(`/api/sources/${s.id}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled: !s.enabled }),
    });
    mutate();
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Sources</h1>
        <button className="btn-ghost" onClick={scrapeAll}>Scrape all now</button>
      </div>

      <form onSubmit={add} className="card p-4 grid grid-cols-1 md:grid-cols-6 gap-3 items-end">
        <div className="md:col-span-2">
          <label className="label">Name</label>
          <input className="input" value={form.name} required
                 onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </div>
        <div className="md:col-span-2">
          <label className="label">URL</label>
          <input className="input" type="url" value={form.url} required
                 placeholder="https://example.com"
                 onChange={(e) => setForm({ ...form, url: e.target.value })} />
        </div>
        <div className="md:col-span-2">
          <label className="label">RSS (optional)</label>
          <input className="input" type="url" value={form.rss_url}
                 placeholder="https://example.com/feed.xml"
                 onChange={(e) => setForm({ ...form, rss_url: e.target.value })} />
        </div>
        <div>
          <label className="label">Mode</label>
          <select className="input" value={form.scrape_mode}
                  onChange={(e) => setForm({ ...form, scrape_mode: e.target.value })}>
            <option value="auto">auto</option>
            <option value="rss">rss</option>
            <option value="http">http</option>
            <option value="playwright">playwright</option>
          </select>
        </div>
        <div className="md:col-span-3">
          <label className="label">Tags (comma-sep)</label>
          <input className="input" value={form.tags}
                 onChange={(e) => setForm({ ...form, tags: e.target.value })} />
        </div>
        <button className="btn-primary md:col-span-2" disabled={busy}>
          {busy ? "Adding…" : "Add source"}
        </button>
      </form>

      <div className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-100 text-left text-xs uppercase text-slate-500">
            <tr>
              <th className="p-3">Name</th>
              <th className="p-3">URL</th>
              <th className="p-3">Mode</th>
              <th className="p-3">Last scraped</th>
              <th className="p-3">Status</th>
              <th className="p-3">Actions</th>
            </tr>
          </thead>
          <tbody>
            {(data ?? []).map((s) => (
              <tr key={s.id} className="border-t border-slate-200">
                <td className="p-3 font-medium">{s.name}</td>
                <td className="p-3"><a className="text-brand-600 break-all" href={s.url} target="_blank">{s.url}</a></td>
                <td className="p-3">{s.scrape_mode}</td>
                <td className="p-3 text-xs">{s.last_scraped_at?.slice(0, 16).replace("T", " ") ?? "—"}</td>
                <td className="p-3">
                  <span className={`badge ${s.enabled ? "bg-emerald-100 text-emerald-800" : "bg-slate-200 text-slate-600"}`}>
                    {s.enabled ? "enabled" : "disabled"}
                  </span>
                </td>
                <td className="p-3 space-x-2">
                  <button className="btn-ghost" onClick={() => scrape(s.id)}>Scrape</button>
                  <button className="btn-ghost" onClick={() => toggle(s)}>{s.enabled ? "Disable" : "Enable"}</button>
                  <button className="btn-danger" onClick={() => del(s.id)}>Delete</button>
                </td>
              </tr>
            ))}
            {data?.length === 0 && (
              <tr><td colSpan={6} className="p-6 text-center text-slate-500">No sources yet — add your first above.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
