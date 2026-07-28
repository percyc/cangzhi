import { NextRequest, NextResponse } from 'next/server';

// Public routes that anyone can visit. The middleware only redirects
// browser navigation; data requests are protected by the API.
const PUBLIC_ROUTES = new Set(['/login', '/setup']);
const PUBLIC_API_PREFIXES = ['/api/auth/', '/api/liveness', '/api/readiness', '/api/health'];

function isPublicPath(pathname: string): boolean {
  if (PUBLIC_ROUTES.has(pathname)) return true;
  return PUBLIC_API_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

function wantsHtml(request: NextRequest): boolean {
  const accept = request.headers.get('accept') ?? '';
  return accept.includes('text/html') || accept.includes('application/xhtml+xml');
}

export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (isPublicPath(pathname)) {
    return NextResponse.next();
  }
  if (!wantsHtml(request)) {
    // Data requests: do not redirect, the API is the final gate.
    return NextResponse.next();
  }
  // Browser navigation: ask the API whether a session is present
  // and forward the result. The proxy never decides auth itself; it
  // only chooses where to send the user.
  const cookieHeader = request.headers.get('cookie');
  try {
    const apiUrl = new URL('/api/auth/status', request.url);
    const response = await fetch(apiUrl, {
      headers: cookieHeader ? { cookie: cookieHeader } : undefined,
      cache: 'no-store',
    });
    if (response.ok) {
      const data = (await response.json()) as {
        authenticated: boolean;
        setup_required: boolean;
      };
      if (data.setup_required) {
        const target = new URL('/setup', request.url);
        return NextResponse.redirect(target);
      }
      if (data.authenticated) {
        return NextResponse.next();
      }
    }
  } catch {
    // Keep the page reachable so it can show a useful connection
    // error instead of bouncing between redirects.
    return NextResponse.next();
  }
  const target = new URL('/login', request.url);
  target.searchParams.set('next', `${request.nextUrl.pathname}${request.nextUrl.search}`);
  return NextResponse.redirect(target);
}

export const config = {
  matcher: [
    // Run on every page route except Next.js internals and static
    // assets. Data routes are filtered out by ``wantsHtml`` above.
    '/((?!_next/static|_next/image|favicon.ico|.*\\.).*)',
  ],
};
