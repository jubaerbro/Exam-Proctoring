import type { Config } from 'tailwindcss';

export default {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: { DEFAULT: '#111827', muted: '#4b5563', faint: '#9ca3af' },
        surface: { DEFAULT: '#ffffff', sunken: '#f9fafb', border: '#e5e7eb' },
        accent: { DEFAULT: '#1d4ed8', hover: '#1e40af', soft: '#eff6ff' },
        warn: { DEFAULT: '#b45309', soft: '#fffbeb' },
        danger: { DEFAULT: '#b91c1c', soft: '#fef2f2' },
        ok: { DEFAULT: '#15803d', soft: '#f0fdf4' },
      },
    },
  },
  plugins: [],
} satisfies Config;
