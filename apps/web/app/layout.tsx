import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: '藏知 - 个人知识库',
  description: '低打扰、自动沉淀的个人知识库',
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  )
}
