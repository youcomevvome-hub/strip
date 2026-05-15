"use client";
import useSWR from "swr";
import { useState } from "react";
import { api, fetcher } from "@/lib/api";

type Delivery = { id: number; platform: string; status: string; external_url?: string | null; error?: string | null };
type Post = {
  id: number; article_id: number; title: string; summary: string; body: string;
  bullets: string[]; hashtags: string[]; links: string[]; image_url?: string | null;
  variants: Record<string, string>; status: string;
  created_at: string; updated_at: string; deliveries: Delivery[];
};

const PLATFORMS = ["twitter","linkedin","facebook","instagram","whatsapp","reddit","telegram","discord","mastodon"];

export default function QueuePage() {
  const { data: posts, mutate } = useSWR<Post[]>("/api/posts?status=drafted", fetcher);
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Validation queue</h1>
      <p className="text-sm text-slate-600">Review AI-structured drafts, edit if needed, approve, then publish.</p>
      <div className="space-y-4">
        {(posts ?? []).map((p) => <PostCard key={p.id} post={p} refresh={mutate} />)}
        {posts?.length === 0 && (
          <div className="card p-6 text-center text-slate-500">
            Nothing pending. Trigger a scrape on the Sources page.
          </div>
        )}
      </div>
    </div>
  );
}

function PostCard({ post, refresh }: { post: Post; refresh: () => void }) {
  const [draft, setDraft] = useState(post);
  const [selected, setSelected] = useState<string[]>(PLATFORMS);
  const [tab, setTab] = useState<string>("summary");
  const [saving, setSaving] = useState(false);

  async function save() {
    setSaving(true);
    try {
      await api(`/api/posts/${post.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          title: draft.title,
          summary: draft.summary,
          body: draft.body,
          bullets: draft.bullets,
          hashtags: draft.hashtags,
          links: draft.links,
          variants: draft.variants,
        }),
      });
    } finally { setSaving(false); }
  }

  async function approveAndPublish() {
    await save();
    await api(`/api/posts/${post.id}/approve`, { method: "POST" });
    await api(`/api/posts/${post.id}/publish`, {
      method: "POST",
      body: JSON.stringify({ platforms: selected }),
    });
    refresh();
  }

  async function reject() {
    await api(`/api/posts/${post.id}/reject`, { method: "POST" });
    refresh();
  }

  function setVariant(k: string, v: string) {
    setDraft({ ...draft, variants: { ...draft.variants, [k]: v } });
  }

  return (
    <div className="card p-5 space-y-4">
      <div className="flex items-start gap-4">
        {draft.image_url && <img src={draft.image_url} alt="" className="w-28 h-28 object-cover rounded" />}
        <div className="flex-1">
          <input
            className="input text-lg font-semibold"
            value={draft.title}
            onChange={(e) => setDraft({ ...draft, title: e.target.value })}
          />
          <div className="text-xs text-slate-500 mt-1">
            Source: {draft.links?.[0] && <a href={draft.links[0]} target="_blank" className="text-brand-600 break-all">{draft.links[0]}</a>}
          </div>
        </div>
      </div>

      <div className="flex gap-1 flex-wrap border-b">
        {["summary", "body", "bullets", "hashtags", ...PLATFORMS].map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-3 py-1.5 text-xs rounded-t ${tab === t ? "bg-slate-100 border border-b-white" : "text-slate-500"}`}
          >{t}</button>
        ))}
      </div>

      {tab === "summary" && (
        <textarea className="input h-24" value={draft.summary}
                  onChange={(e) => setDraft({ ...draft, summary: e.target.value })} />
      )}
      {tab === "body" && (
        <textarea className="input h-64 font-mono text-xs" value={draft.body}
                  onChange={(e) => setDraft({ ...draft, body: e.target.value })} />
      )}
      {tab === "bullets" && (
        <textarea className="input h-32" value={draft.bullets.join("\n")}
                  onChange={(e) => setDraft({ ...draft, bullets: e.target.value.split("\n").filter(Boolean) })} />
      )}
      {tab === "hashtags" && (
        <input className="input" value={draft.hashtags.join(" ")}
               onChange={(e) => setDraft({ ...draft, hashtags: e.target.value.split(/\s+/).filter(Boolean) })} />
      )}
      {PLATFORMS.includes(tab) && (
        <div>
          <label className="label">
            {tab} variant {tab === "instagram" && draft.image_url ? "" : tab === "instagram" ? "(needs image)" : ""}
          </label>
          <textarea
            className="input h-32"
            value={draft.variants?.[tab === "reddit" ? "reddit_body" : tab] ?? ""}
            onChange={(e) => setVariant(tab === "reddit" ? "reddit_body" : tab, e.target.value)}
          />
          {tab === "reddit" && (
            <>
              <label className="label mt-2">Reddit title</label>
              <input className="input"
                     value={draft.variants?.reddit_title ?? ""}
                     onChange={(e) => setVariant("reddit_title", e.target.value)} />
            </>
          )}
        </div>
      )}

      <div className="border-t pt-4">
        <div className="text-xs font-medium text-slate-600 mb-2">Publish to:</div>
        <div className="flex flex-wrap gap-2">
          {PLATFORMS.map((p) => (
            <label key={p} className={`px-3 py-1 rounded border cursor-pointer text-xs ${
              selected.includes(p) ? "bg-brand-600 text-white border-brand-600" : "bg-white border-slate-300"
            }`}>
              <input type="checkbox" className="hidden"
                     checked={selected.includes(p)}
                     onChange={(e) => setSelected(e.target.checked
                       ? [...selected, p]
                       : selected.filter(x => x !== p))} />
              {p}
            </label>
          ))}
        </div>
      </div>

      <div className="flex gap-2 justify-end border-t pt-4">
        <button className="btn-danger" onClick={reject}>Reject</button>
        <button className="btn-ghost" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save draft"}</button>
        <button className="btn-primary" onClick={approveAndPublish}>Approve & Publish</button>
      </div>
    </div>
  );
}
