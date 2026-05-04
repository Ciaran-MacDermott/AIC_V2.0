import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50:  "#F5F0FA",
          100: "#E8DBF2",
          200: "#D2BAE5",
          300: "#B594D2",
          400: "#8B5DAF",
          500: "#6A1A94",
          600: "#4E106F",
          700: "#3A0D54",
          800: "#2E0840",
          900: "#1F052B",
        },
        // Semantic colours from the Streamlit app's brand palette.
        ok:    "#059669",
        okd:   "#047857",
        warn:  "#D97706",
        err:   "#DC2626",
        edit:  "#F3E8FF",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Consolas", "monospace"],
      },
      boxShadow: {
        card: "0 1px 2px rgba(15, 23, 42, 0.04)",
        "card-hover": "0 4px 16px rgba(15, 23, 42, 0.06), 0 1px 3px rgba(15, 23, 42, 0.04)",
      },
    },
  },
  plugins: [],
};

export default config;
