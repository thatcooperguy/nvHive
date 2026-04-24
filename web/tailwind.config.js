/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './app/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './lib/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // NVIDIA corporate white theme
        background: {
          DEFAULT: '#ffffff',
          secondary: '#fafafa',
          tertiary: '#f5f5f5',
          card: '#ffffff',
          code: '#0d0d0d', // dark code surface
        },
        border: {
          DEFAULT: '#e5e5e5',
          subtle: '#eeeeee',
          bright: '#d4d4d4',
          strong: '#a3a3a3',
        },
        accent: {
          green: '#76B900', // NVIDIA green
          'green-dim': '#5a9100',
          'green-deep': '#447000',
          blue: '#2563eb',
          'blue-dim': '#1d4ed8',
          'blue-glow': '#3b82f6',
          amber: '#d97706',
          'amber-dim': '#b45309',
          red: '#dc2626',
          'red-dim': '#b91c1c',
          purple: '#7c3aed',
          'purple-dim': '#6d28d9',
          cyan: '#0891b2',
        },
        text: {
          primary: '#0a0a0a',
          secondary: '#404040',
          muted: '#737373',
          faint: '#a3a3a3',
          inverse: '#ffffff',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'Consolas', 'monospace'],
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'fade-in': 'fadeIn 0.3s ease-in-out',
        'slide-up': 'slideUp 0.3s ease-out',
        'glow': 'glow 2s ease-in-out infinite alternate',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        slideUp: {
          '0%': { opacity: '0', transform: 'translateY(10px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        glow: {
          '0%': { boxShadow: '0 0 5px rgba(118, 185, 0, 0.3)' },
          '100%': { boxShadow: '0 0 20px rgba(118, 185, 0, 0.6)' },
        },
      },
      boxShadow: {
        'glow-green': '0 4px 16px rgba(118, 185, 0, 0.18)',
        'glow-blue': '0 4px 16px rgba(37, 99, 235, 0.18)',
        'glow-amber': '0 4px 16px rgba(217, 119, 6, 0.18)',
        'card': '0 1px 3px rgba(0, 0, 0, 0.05), 0 1px 2px rgba(0, 0, 0, 0.03)',
        'card-hover': '0 8px 24px rgba(0, 0, 0, 0.08)',
      },
    },
  },
  plugins: [],
};
