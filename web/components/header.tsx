"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/**
 * Two-row header: a slim sticky app bar with the Circana wordmark + a
 * phase nav, plus a per-page hero that picks up its title/subtitle
 * from props.  Replaces the older full-width gradient hero — same
 * brand colour story, modern Linear/Vercel-style chrome.
 */

const NAV: { href: string; label: string }[] = [
  { href: "/",       label: "Phase 1" },
  { href: "/phase2", label: "Phase 2 & 3" },
];

function Wordmark() {
  // Real Circana logo — 420×122 source, served from /public so the
  // Next static export bundles it.  We render at 28px tall so it sits
  // cleanly in the 56px app bar.
  return (
    <Link href="/" className="flex items-center gap-3 group" aria-label="Circana — AIC home">
      <img
        src="/Circana_logo.png"
        alt="Circana"
        className="h-9 w-auto select-none"
        draggable={false}
      />
      <span className="hidden sm:flex items-baseline gap-1.5 text-[19px] font-semibold tracking-tight">
        <span className="text-zinc-300">/</span>
        <span className="text-brand-700">Assortment AIC</span>
      </span>
    </Link>
  );
}

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
      <div className="app-bar">
        <div className="mx-auto max-w-5xl px-6 h-[70px] flex items-center justify-between">
          <Wordmark />
          <NavTabs pathname={pathname} />
        </div>
      </div>

      <section className="mx-auto max-w-5xl px-6 pt-12 pb-7 fade-in-up">
        {eyebrow && (
          <p className="text-[12px] font-semibold uppercase tracking-[0.14em] text-brand-700/80 mb-2">
            {eyebrow}
          </p>
        )}
        <h1 className="text-[36px] sm:text-[42px] font-semibold tracking-tight text-zinc-900 leading-tight">
          {title}
        </h1>
        {subtitle && (
          <p className="mt-3 text-[17px] text-zinc-600 max-w-2xl leading-relaxed whitespace-pre-line">
            {subtitle}
          </p>
        )}
      </section>
    </>
  );
}
