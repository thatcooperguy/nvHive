import type { Metadata } from 'next';
import './globals.css';
import LayoutShell from '@/components/LayoutShell';

export const metadata: Metadata = {
  title: 'NVHive - NVIDIA AI Workspace',
  description: 'NVIDIA-styled multi-LLM orchestration platform for local GPU and cloud advisor workflows.',
  keywords: ['AI', 'NVIDIA', 'Nemotron', 'LLM', 'GPU', 'local AI', 'Ollama'],
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{
          __html: `window.__HIVE_API_URL__ = ${JSON.stringify(process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000')};`
        }} />
        {/*
          Inline pre-hydration theme bootstrap. Reads nvh_theme from
          localStorage (or falls back to prefers-color-scheme) and toggles
          .dark on <html> before React paints, so the page never flashes
          light-mode for dark-mode users.
        */}
        <script dangerouslySetInnerHTML={{
          __html: `(function(){try{var m=localStorage.getItem('nvh_theme');var d=m==='dark'||((m==='system'||!m)&&window.matchMedia('(prefers-color-scheme: dark)').matches);if(d)document.documentElement.classList.add('dark');}catch(e){}})();`
        }} />
      </head>
      <body className="antialiased" style={{ background: 'var(--bg-primary)', color: 'var(--text-primary)' }}>
        <LayoutShell>
          {children}
        </LayoutShell>
      </body>
    </html>
  );
}
