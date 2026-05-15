"use client";
import { useState } from "react";
import { api } from "@/lib/api";
import { useRouter } from "next/navigation";

export default function RegisterPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    try {
      await api("/api/auth/register", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      router.push("/login");
    } catch (e: any) { setErr(e.message); }
  }

  return (
    <div className="max-w-sm mx-auto mt-20 card p-6">
      <h1 className="text-xl font-bold mb-4">Create admin account</h1>
      <form onSubmit={submit} className="space-y-3">
        <div>
          <label className="label">Email</label>
          <input className="input" type="email" value={email}
                 onChange={(e) => setEmail(e.target.value)} required />
        </div>
        <div>
          <label className="label">Password (min 6)</label>
          <input className="input" type="password" value={password}
                 onChange={(e) => setPassword(e.target.value)} required minLength={6} />
        </div>
        {err && <div className="text-sm text-red-600">{err}</div>}
        <button className="btn-primary w-full">Create account</button>
      </form>
    </div>
  );
}
