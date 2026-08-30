import type { Metadata, Viewport } from 'next';

import { TopNav } from '@/components/TopNav';

import './globals.css';

const basePath = process.env.NEXT_PUBLIC_CANGZHI_WEB_BASE_PATH ?? '';

// Existing API callers intentionally use root-relative /api URLs. When this
// build is mounted below DSH, rewrite those browser requests before any client
// component hydrates. Next itself owns basePath handling for Link and Router.
const gatewayBootstrap = basePath
  ? `(()=>{const p=${JSON.stringify(basePath)};const map=u=>{try{const x=new URL(u,location.href);if(x.origin===location.origin&&x.pathname.startsWith('/api/'))return p+x.pathname+x.search+x.hash}catch{}return u};const f=window.fetch.bind(window);window.fetch=(i,n)=>{if(typeof i==='string'||i instanceof URL)return f(map(String(i)),n);if(i instanceof Request){const u=map(i.url);return f(u===i.url?i:new Request(u,i),n)}return f(i,n)};const o=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u,...r){return o.call(this,m,map(String(u)),...r)};addEventListener('click',e=>{const a=e.target instanceof Element?e.target.closest('a'):null;if(!a)return;const h=a.getAttribute('href');if(h&&h.startsWith('/api/'))a.setAttribute('href',p+h)},true)})();`
  : '';

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
      {gatewayBootstrap && (
        <head>
          <script dangerouslySetInnerHTML={{ __html: gatewayBootstrap }} />
        </head>
      )}
      <body>
        <TopNav />
        {children}
      </body>
    </html>
  );
}
