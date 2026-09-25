import type { Config } from 'tailwindcss';
import tailwindcssAnimate from 'tailwindcss-animate';

// Tokens mirror Aegis_UIUX_Spec section 94. Colours are declared as CSS
// variables in globals.css so the design system owns semantic rendering and
// components never hardcode a hex value.
const config: Config = {
  darkMode: 'class',
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        canvas: 'var(--bg)',
        surface: {
          1: 'var(--surface-1)',
          2: 'var(--surface-2)',
          3: 'var(--surface-3)',
          4: 'var(--surface-4)',
        },
        ink: {
          primary: 'var(--text-primary)',
          secondary: 'var(--text-secondary)',
          tertiary: 'var(--text-tertiary)',
        },
        hairline: 'var(--border-subtle)',
        line: 'var(--border-default)',
        edge: 'var(--border-strong)',
        status: {
          critical: 'var(--status-critical)',
          warning: 'var(--status-warning)',
          info: 'var(--status-info)',
          success: 'var(--status-success)',
          neutral: 'var(--status-neutral)',
        },
        accent: 'var(--accent)',
      },
      fontFamily: {
        sans: ['var(--font-sans)'],
        mono: ['var(--font-mono)'],
      },
      fontSize: {
        meta: ['0.68rem', { lineHeight: '1rem', letterSpacing: '0.02em' }],
        body: ['0.82rem', { lineHeight: '1.25rem' }],
        h3: ['0.9rem', { lineHeight: '1.3rem' }],
        h2: ['1.25rem', { lineHeight: '1.6rem' }],
        h1: ['2.75rem', { lineHeight: '1.05' }],
      },
      borderRadius: {
        btn: '7px',
        card: '11px',
        drawer: '14px',
      },
      transitionDuration: {
        hover: '140ms',
        drawer: '200ms',
      },
      keyframes: {
        'pulse-soft': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.45' },
        },
        'edge-flow': {
          to: { strokeDashoffset: '-16' },
        },
      },
      animation: {
        'pulse-soft': 'pulse-soft 2s ease-in-out infinite',
        'edge-flow': 'edge-flow 1s linear infinite',
      },
    },
  },
  plugins: [tailwindcssAnimate],
};
export default config;
