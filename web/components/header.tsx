"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { AppBar, PageHeader, Wordmark } from "@/kit";

/**
 * AIC's app header — kit's AppBar + PageHeader, with an app-specific
 * NavTabs as the AppBar center slot. The Wordmark + page hero shape are
 * shared with data_ingester and ADB-TOOL-v2; only NavTabs is local.
 */

const NAV: { href: string; label: string }[] = [
  { href: "/",       label: "Phase 1" },
  { href: "/phase2", label: "Phase 2 & 3" },
];

function NavTabs({ pathname }: { pathname: string }) {
  return (
    <nav className="flex items-center gap-1 text-[15px]">
      {NAV.map((item) => {
        const active =
          item.href === "/"
            ? pathname === "/" || pathname === ""
            : pathname.startsWith(item.href);
        return (
          <Link
            key={item.href}
            href={item.href}
            className={
              active
                ? "rounded-md px-3 py-1.5 bg-brand-50 text-brand-700 font-medium"
                : "rounded-md px-3 py-1.5 text-zinc-600 hover:text-zinc-900 hover:bg-zinc-100 transition-colors"
            }
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

export function Header({
  title    = "Attribute Mapping",
  subtitle = "Lookup → BM25 → XGBoost ensemble. Review your QC sheets, finalise, export.",
  eyebrow,
}: {
  title?:    string;
  subtitle?: string;
  eyebrow?:  string;
}) {
  const pathname = usePathname() ?? "/";

  return (
    <>
      <AppBar
        left={
          <Link href="/" aria-label="Circana — Assortment AIC home">
            <Wordmark tag="Assortment AIC" />
          </Link>
        }
        center={<NavTabs pathname={pathname} />}
      />
      <div className="mx-auto max-w-5xl px-6">
        <PageHeader title={title} subtitle={subtitle} eyebrow={eyebrow} />
      </div>
    </>
  );
}
