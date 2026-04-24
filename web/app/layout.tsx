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
    <html lang="en">
      <head>
        <script dangerouslySetInnerHTML={{
          __html: `window.__HIVE_API_URL__ = "${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'}";`
        }} />
      </head>
      <body className="bg-white text-[#0a0a0a] antialiased">
        <LayoutShell>
          {children}
        </LayoutShell>
      </body>
    </html>
  );
}
