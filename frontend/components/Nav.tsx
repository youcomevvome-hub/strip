"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { setToken, getToken } from "@/lib/api";
import { useEffect, useState } from "react";

const tabs = [
  { href: "/", label: "Dashboard" },
  { href: "/sources", label: "Sources" },
  { href: "/queue", label: "Validation Queue" },
  { href: "/posts", label: "Posts" },
  { href: "/settings", label: "Settings" },
];

export function Nav() {
  const pathname = usePathname();
  const router = useRouter();
  const [authed, setAuthed] = useState(false);
  useEffect(() => { setAuthed(!!getToken()); }, [pathname]);

  if (pathname === "/login" || pathname === "/register") return null;

  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="max-w-6xl mx-auto flex items-center gap-6 px-6 h-14">
        <Link href="/" className="font-bold text-brand-600">Strip</Link>
        <nav className="flex gap-1 flex-1">
          {tabs.map((t) => (
            <Link
              key={t.href}
              href={t.href}
              className={`px-3 py-1.5 rounded text-sm ${
                pathname === t.href ? "bg-brand-600 text-white" : "hover:bg-slate-100"
              }`}
            >
              {t.label}
            </Link>
          ))}
        </nav>
        {authed && (
          <button
            className="btn-ghost"
            onClick={() => { setToken(null); router.push("/login"); }}
          >
            Logout
          </button>
        )}
      </div>
    </header>
  );
}
