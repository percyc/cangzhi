import { NextResponse } from 'next/server';
import { WEB_BUILD } from '@/lib/build-info';

export async function GET() {
  return NextResponse.json({ status: 'ok', build: WEB_BUILD }, { status: 200, headers: { 'Cache-Control': 'no-store' } });
}
