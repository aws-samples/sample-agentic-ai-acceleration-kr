import plugin from "tailwindcss/plugin";
import containerQueries from "@tailwindcss/container-queries";
import typography from "@tailwindcss/typography";
import forms from "@tailwindcss/forms";
import tailwindcssAnimate from "tailwindcss-animate";
import headlessui from "@headlessui/tailwindcss";

/**
 * Every colour resolves to an HSL triple in globals.css, so opacity modifiers
 * (`bg-primary/10`, `border-border/60`) work everywhere.
 *
 * There used to be a second, parallel token layer here — `backgroundColor`,
 * `textColor` and `borderColor` blocks mapping `primary`, `secondary` and
 * friends onto `var(--bg-primary)`, `var(--text-primary)`, `var(--border-primary)`.
 * Those variables were never defined, and because those blocks *override* the
 * `colors` scale for their respective utilities, `bg-primary` resolved to
 * transparent — every filled button rendered white. They are gone; `colors`
 * below is the only mapping.
 */
const withOpacity = (variable) => `hsl(var(${variable}) / <alpha-value>)`;

/** @type {import('tailwindcss').Config} */
export default {
  // `mjs` is not optional here. This project keeps its pure logic in `.mjs`
  // modules so it can be tested with `node --test`, and one of them
  // (`src/app/insights/layoutModel.mjs`) returns Tailwind class names. Without
  // `mjs` in this glob those classes are never generated: measured 2026-08-16,
  // the widget grid rendered every `col-span-2` widget at half width because the
  // class was applied to the element and the rule did not exist —
  // `getComputedStyle(el).gridColumn` read `auto`.
  content: ["./src/**/*.{js,mjs,ts,jsx,tsx}"],
  darkMode: ["class", '[data-joy-color-scheme="dark"]'],
  theme: {
    extend: {
      fontSize: {
        xxs: ["0.6875rem", { lineHeight: "1rem", letterSpacing: "0.01em" }],
        xs: ["0.75rem", { lineHeight: "1.0625rem" }],
        sm: ["0.8125rem", { lineHeight: "1.1875rem" }],
        base: ["0.9375rem", { lineHeight: "1.45rem" }],
        lg: ["1.0625rem", { lineHeight: "1.5rem", letterSpacing: "-0.014em" }],
        xl: ["1.25rem", { lineHeight: "1.75rem", letterSpacing: "-0.02em" }],
        "2xl": ["1.5rem", { lineHeight: "2rem", letterSpacing: "-0.022em" }],
        "3xl": ["1.875rem", { lineHeight: "2.25rem", letterSpacing: "-0.026em" }],
      },
      fontFamily: {
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      spacing: {
        /* Route header height — between h-12 and h-14, which is exactly where
           a one-line header wants to sit. */
        13: "3.25rem",
      },
      letterSpacing: {
        tighter: "-0.032em",
        tight: "-0.02em",
        snug: "-0.01em",
        normal: "0",
        wide: "0.02em",
        wider: "0.05em",
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
        xs: "3px",
      },
      boxShadow: {
        /* Deliberately restrained: surfaces separate by hairline + background
           step, not by drop shadow. Only overlays float. */
        xs: "0 1px 1px 0 hsl(var(--neutral-950) / 0.04)",
        sm: "0 1px 2px 0 hsl(var(--neutral-950) / 0.05)",
        md: "0 2px 4px -1px hsl(var(--neutral-950) / 0.06), 0 1px 2px -1px hsl(var(--neutral-950) / 0.04)",
        lg: "0 8px 24px -6px hsl(var(--neutral-950) / 0.12), 0 2px 6px -2px hsl(var(--neutral-950) / 0.06)",
        xl: "0 20px 48px -12px hsl(var(--neutral-950) / 0.18), 0 4px 12px -4px hsl(var(--neutral-950) / 0.08)",
        /* Inset top highlight — gives filled buttons a subtle bevel. */
        raised:
          "inset 0 1px 0 0 hsl(0 0% 100% / 0.09), 0 1px 2px 0 hsl(var(--neutral-950) / 0.10)",
      },
      colors: {
        brand: {
          50: withOpacity("--brand-50"),
          100: withOpacity("--brand-100"),
          200: withOpacity("--brand-200"),
          300: withOpacity("--brand-300"),
          400: withOpacity("--brand-400"),
          500: withOpacity("--brand-500"),
          600: withOpacity("--brand-600"),
          700: withOpacity("--brand-700"),
          800: withOpacity("--brand-800"),
          900: withOpacity("--brand-900"),
          950: withOpacity("--brand-950"),
        },
        neutral: {
          50: withOpacity("--neutral-50"),
          100: withOpacity("--neutral-100"),
          200: withOpacity("--neutral-200"),
          300: withOpacity("--neutral-300"),
          400: withOpacity("--neutral-400"),
          500: withOpacity("--neutral-500"),
          550: withOpacity("--neutral-550"),
          600: withOpacity("--neutral-600"),
          700: withOpacity("--neutral-700"),
          800: withOpacity("--neutral-800"),
          900: withOpacity("--neutral-900"),
          950: withOpacity("--neutral-950"),
        },
        border: {
          DEFAULT: withOpacity("--border"),
          strong: withOpacity("--border-strong"),
        },
        /* Categorical series colours. Charts read these through `hsl(var(--chart-N))`
           directly — recharts takes `fill`/`stroke` strings, not class names — so
           this mapping exists for the surrounding chrome (legend swatches, a
           colour-matched label) that is ordinary markup. */
        chart: {
          1: withOpacity("--chart-1"),
          2: withOpacity("--chart-2"),
          3: withOpacity("--chart-3"),
          4: withOpacity("--chart-4"),
          5: withOpacity("--chart-5"),
          6: withOpacity("--chart-6"),
          7: withOpacity("--chart-7"),
          8: withOpacity("--chart-8"),
        },
        input: withOpacity("--input"),
        ring: withOpacity("--ring"),
        background: withOpacity("--background"),
        foreground: withOpacity("--foreground"),
        primary: {
          DEFAULT: withOpacity("--primary"),
          foreground: withOpacity("--primary-foreground"),
          hover: withOpacity("--primary-hover"),
          tint: withOpacity("--primary-tint"),
        },
        secondary: {
          DEFAULT: withOpacity("--secondary"),
          foreground: withOpacity("--secondary-foreground"),
        },
        destructive: {
          DEFAULT: withOpacity("--destructive"),
          foreground: withOpacity("--destructive-foreground"),
        },
        success: {
          DEFAULT: withOpacity("--success"),
          foreground: withOpacity("--success-foreground"),
        },
        warning: {
          DEFAULT: withOpacity("--warning"),
          foreground: withOpacity("--warning-foreground"),
        },
        info: {
          DEFAULT: withOpacity("--info"),
          foreground: withOpacity("--info-foreground"),
        },
        muted: {
          DEFAULT: withOpacity("--muted"),
          foreground: withOpacity("--muted-foreground"),
        },
        accent: {
          DEFAULT: withOpacity("--accent"),
          foreground: withOpacity("--accent-foreground"),
        },
        popover: {
          DEFAULT: withOpacity("--popover"),
          foreground: withOpacity("--popover-foreground"),
        },
        card: {
          DEFAULT: withOpacity("--card"),
          foreground: withOpacity("--card-foreground"),
        },
        sidebar: {
          DEFAULT: withOpacity("--sidebar"),
        },
      },
      keyframes: {
        hide: {
          from: { opacity: 1 },
          to: { opacity: 0 },
        },
        slideIn: {
          from: { transform: "translateX(calc(100% + var(--viewport-padding)))" },
          to: { transform: "translateX(0)" },
        },
        swipeOut: {
          from: { transform: "translateX(var(--radix-toast-swipe-end-x))" },
          to: { transform: "translateX(calc(100% + var(--viewport-padding)))" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        hide: "hide 100ms ease-in",
        slideIn: "slideIn 150ms cubic-bezier(0.16, 1, 0.3, 1)",
        swipeOut: "swipeOut 100ms ease-out",
        shimmer: "shimmer 1.6s infinite",
      },
      transitionTimingFunction: {
        /* Fast out, settle in — the house easing for hovers and reveals. */
        snap: "cubic-bezier(0.16, 1, 0.3, 1)",
      },
    },
    typography: {
      playground: {
        css: {
          "h1, h2, h3, h4, h5, h6": { fontWeight: "600" },
          h1: { fontSize: "1.375rem" },
          h2: { fontSize: "1.1875rem" },
          h3: { fontSize: "1.0625rem" },
          h4: { fontSize: "0.9375rem" },
          h5: { fontSize: "0.875rem" },
          h6: { fontSize: "0.8125rem" },
          ul: {
            marginLeft: "1.125rem !important",
            listStyleType: "disc !important",
          },
          ol: {
            marginLeft: "1.125rem !important",
            listStyleType: "decimal !important",
          },
          a: {
            color: "hsl(var(--primary))",
            textDecoration: "underline",
            textUnderlineOffset: "2px",
          },
          table: {
            width: "100%",
            borderCollapse: "collapse",
            th: {
              padding: "0.4375rem 0.625rem",
              borderBottom: "1px solid hsl(var(--border-strong))",
              fontWeight: "600",
              textAlign: "left",
              fontSize: "0.75rem",
              letterSpacing: "0.04em",
              textTransform: "uppercase",
              color: "hsl(var(--muted-foreground))",
            },
            td: {
              padding: "0.4375rem 0.625rem",
              borderBottom: "1px solid hsl(var(--border))",
            },
          },
          blockquote: {
            borderLeft: "2px solid hsl(var(--primary) / 0.4)",
            paddingLeft: "0.875rem",
            marginLeft: "0",
            color: "hsl(var(--muted-foreground))",
          },
          "s, strike, del": { textDecoration: "line-through" },
        },
      },
    },
  },
  plugins: [
    containerQueries,
    typography,
    forms,
    tailwindcssAnimate,
    headlessui,
    plugin(({ addUtilities, addBase }) => {
      addBase({
        input: {
          borderWidth: "0",
          padding: "0",
        },
        "html, body, *": {
          "scrollbar-width": "thin",
          "scrollbar-color": "hsl(var(--scrollbar-thumb)) transparent",
        },
        "*::-webkit-scrollbar": {
          width: "8px",
          height: "8px",
          background: "transparent",
        },
        "*::-webkit-scrollbar-track": {
          background: "transparent",
        },
        "*::-webkit-scrollbar-thumb": {
          background: "hsl(var(--scrollbar-thumb))",
          "border-radius": "9999px",
        },
        "*::-webkit-scrollbar-thumb:hover": {
          background: "hsl(var(--scrollbar-thumb-hover))",
        },
      });

      addUtilities({
        ".no-scrollbar": {
          "scrollbar-width": "none",
          "&::-webkit-scrollbar": { display: "none" },
        },
      });

      // https://github.com/tailwindlabs/tailwindcss/discussions/12127
      addUtilities({
        ".break-anywhere": {
          "@supports (overflow-wrap: anywhere)": {
            "overflow-wrap": "anywhere",
          },
          "@supports not (overflow-wrap: anywhere)": {
            "word-break": "break-word",
          },
        },
      });

      addUtilities({
        ".no-number-spinner": {
          MozAppearance: "textfield",
          "&::-webkit-outer-spin-button": {
            WebkitAppearance: "none !important",
            margin: 0,
          },
          "&::-webkit-inner-spin-button": {
            WebkitAppearance: "none !important",
            margin: 0,
          },
        },
      });

      addUtilities({
        ".text-security": {
          textSecurity: "disc",
          WebkitTextSecurity: "disc",
          MozTextSecurity: "disc",
        },
      });

      addUtilities({
        ".display-sm": {
          fontSize: "0.9375rem",
          lineHeight: "1.375rem",
          fontWeight: "600",
          letterSpacing: "-0.011em",
        },
        ".display-base": {
          fontSize: "1.25rem",
          lineHeight: "1.75rem",
          fontWeight: "600",
          letterSpacing: "-0.02em",
        },
        ".display-lg": {
          fontSize: "1.5rem",
          lineHeight: "2rem",
          fontWeight: "600",
          letterSpacing: "-0.024em",
        },
        ".display-xl": {
          fontSize: "1.875rem",
          lineHeight: "2.25rem",
          fontWeight: "600",
          letterSpacing: "-0.028em",
        },
        ".display-2xl": {
          fontSize: "2.5rem",
          lineHeight: "2.875rem",
          fontWeight: "600",
          letterSpacing: "-0.032em",
        },
        /* Small uppercase eyebrow labels — section headers, table headers,
           metadata keys. Kept as a utility so the tracking stays consistent. */
        ".caps-label-sm": {
          fontSize: "0.75rem",
          lineHeight: "1rem",
          letterSpacing: "0.05em",
          textTransform: "uppercase",
          fontWeight: "600",
        },
        ".caps-label-xs": {
          fontSize: "0.6875rem",
          lineHeight: "0.9375rem",
          letterSpacing: "0.055em",
          textTransform: "uppercase",
          fontWeight: "600",
        },
      });
    }),
  ],
};
