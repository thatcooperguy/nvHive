import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    apiUrl: process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8000',
  });
}
