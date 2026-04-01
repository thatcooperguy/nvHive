import type { Metadata } from 'next';
import './globals.css';
import LayoutShell from '@/components/LayoutShell';

export const metadata: Metadata = {
  title: 'Hive — AI Command Center',
  description: 'NVIDIA-powered multi-LLM orchestration platform. Run Nemotron and other AI models locally on your GPU.',
  keywords: ['AI', 'NVIDIA', 'Nemotron', 'LLM', 'GPU', 'local AI', 'Ollama'],
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <head>
        <script dangerouslySetInnerHTML={{
          __html: `window.__HIVE_API_URL__ = "${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'}";`
        }} />
      </head>
      <body className="bg-[#0a0a0a] text-white antialiased">
        <LayoutShell>
          {children}
        </LayoutShell>
      </body>
    </html>
  );
}
