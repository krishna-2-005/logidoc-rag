import type { Config } from "tailwindcss"

// The design tokens live in src/index.css as CSS variables (shadcn/ui style),
// so the palette is defined in one place and checked for contrast there.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: { DEFAULT: "hsl(var(--primary))", foreground: "hsl(var(--primary-foreground))" },
        secondary: { DEFAULT: "hsl(var(--secondary))", foreground: "hsl(var(--secondary-foreground))" },
        muted: { DEFAULT: "hsl(var(--muted))", foreground: "hsl(var(--muted-foreground))" },
        accent: { DEFAULT: "hsl(var(--accent))", foreground: "hsl(var(--accent-foreground))" },
        destructive: { DEFAULT: "hsl(var(--destructive))", foreground: "hsl(var(--destructive-foreground))" },
        card: { DEFAULT: "hsl(var(--card))", foreground: "hsl(var(--card-foreground))" },
        navy: {
          DEFAULT: "hsl(var(--navy))",
          deep: "hsl(var(--navy-deep))",
          line: "hsl(var(--navy-line))",
          text: "hsl(var(--navy-text))",
        },
        ok: { DEFAULT: "hsl(var(--ok))", soft: "hsl(var(--ok-soft))" },
        warn: { DEFAULT: "hsl(var(--warn))", soft: "hsl(var(--warn-soft))" },
        bad: { DEFAULT: "hsl(var(--bad))", soft: "hsl(var(--bad-soft))" },
        info: { DEFAULT: "hsl(var(--info))", soft: "hsl(var(--info-soft))" },
      },
      borderRadius: { lg: "0.625rem", md: "0.5rem", sm: "0.375rem" },
      fontFamily: {
        sans: ['"Inter"', "ui-sans-serif", "system-ui", "-apple-system", '"Segoe UI"', "Roboto", "sans-serif"],
        mono: ["ui-monospace", '"Cascadia Code"', "Menlo", "Consolas", "monospace"],
      },
      boxShadow: { card: "0 1px 2px 0 rgb(15 23 42 / 0.04), 0 1px 3px 0 rgb(15 23 42 / 0.06)" },
    },
  },
  plugins: [],
} satisfies Config
