"use client";

// Tab nav for the /settings section. Restores the import referenced by
// `app/settings/layout.tsx` — the component was missing on main (BA-01).
import Link from "next/link";
import { usePathname } from "next/navigation";

const TABS = [
  { href: "/settings", label: "Instrumente", exact: true },
  { href: "/settings/bo", label: "BOAgents", exact: false },
] as const;

export function SettingsTabNav() {
  const pathname = usePathname();
  return (
    <nav aria-label="Secțiuni Setări" className="flex gap-1 border-b border-line">
      {TABS.map((tab) => {
        const active = tab.exact
          ? pathname === tab.href
          : pathname === tab.href || pathname.startsWith(`${tab.href}/`);
        return (
          <Link
            key={tab.href}
            href={tab.href}
            aria-current={active ? "page" : undefined}
            className={`min-h-touch inline-flex items-center px-3.5 text-sm font-medium border-b-2 -mb-px transition-colors ${
              active
                ? "border-accent text-fg"
                : "border-transparent text-fg-muted hover:text-fg"
            }`}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
