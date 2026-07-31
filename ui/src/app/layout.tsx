import type { Metadata } from 'next';
import { Providers } from '@/components/Providers';
import '@/styles/globals.css';
import '@/styles/index.css';

export const metadata: Metadata = {
  title: 'polyris Console',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <head>
        {/* Apply the persisted theme before first paint so dark mode doesn't flash light
            (the store applies [data-theme] only after hydration). */}
        <script
          dangerouslySetInnerHTML={{
            __html: `try{var t=localStorage.getItem('theme');if(t==='dark'||t==='light')document.documentElement.setAttribute('data-theme',t)}catch(e){}`,
          }}
        />
        {/* Runtime config — loaded before React boots, sets window.CONFIG */}
        {/* In production, deploy-ui.sh generates this with real API Gateway URL */}
        {/* eslint-disable-next-line @next/next/no-sync-scripts */}
        <script src="/config.js" />
        {/* eslint-disable-next-line @next/next/no-page-custom-font -- App Router layout.tsx applies to all pages */}
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <a href="#main-content" className="skip-to-content">Skip to main content</a>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
