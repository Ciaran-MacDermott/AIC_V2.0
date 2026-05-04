import "./globals.css";
import "ag-grid-community/styles/ag-grid.css";
import "ag-grid-community/styles/ag-theme-quartz.css";
import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Inter } from "next/font/google";

// Self-host Inter via next/font/google.  The font files are downloaded
// at build time and emitted under /_next/static/media/, so the runtime
// page never calls fonts.googleapis.com — important for walled-garden
// deployments where outbound HTTPS is blocked.  display:swap matches
// the @import-url behaviour we replaced.
const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700", "800"],
  display: "swap",
  variable: "--font-inter",
});

export const metadata: Metadata = {
  title: "AIC — Attribute Mapping",
  description: "Assortment Intelligence Classifier — Phase 1",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={inter.variable}>
      <body>{children}</body>
    </html>
  );
}
