import { NextRequest, NextResponse } from "next/server";

const API = process.env.API_BASE_URL ?? "http://127.0.0.1:8000";

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const target = `${API}/${path.join("/")}${request.nextUrl.search}`;
  const headers = new Headers();
  for (const name of ["content-type", "cookie", "x-csrf-token"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set("x-forwarded-for", request.headers.get("x-forwarded-for")?.split(",")[0] ?? "127.0.0.1");
  const upstream = await fetch(target, {
    method: request.method,
    headers,
    body: request.method === "GET" || request.method === "HEAD" ? undefined : await request.text(),
    cache: "no-store",
  });
  const response = new NextResponse(upstream.body, { status: upstream.status, headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" } });
  const setCookie = upstream.headers.get("set-cookie");
  if (setCookie) response.headers.set("set-cookie", setCookie);
  return response;
}

export const GET = proxy;
export const POST = proxy;
