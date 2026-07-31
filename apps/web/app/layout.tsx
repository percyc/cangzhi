import type { Metadata, Viewport } from 'next';

import { TopNav } from '@/components/TopNav';

import './globals.css';

export const metadata: Metadata = {
  applicationName: 'Cangzhi',
  title: {
    default: '藏知 Cangzhi｜个人可控的 AI 知识中枢',
    template: '%s｜藏知 Cangzhi',
  },
  description: '藏有所知，问有所据。个人可控的 AI 知识中枢。',
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  viewportFit: 'cover',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <body>
        <TopNav />
        {children}
      </body>
    </html>
  );
}
