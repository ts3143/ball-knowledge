import { NextResponse } from "next/server";
import { ServiceError } from "@/lib/dive/errors";

// Shared by the route handlers: JSON responses, the error envelope {error:{code,message}} and the CSRF guard.

const NO_STORE = { "Cache-Control": "private, no-store" };

export function json(body: unknown, status = 200): NextResponse {
  return NextResponse.json(body, { status, headers: NO_STORE });
}

export function errorResponse(e: unknown): NextResponse {
  if (e instanceof ServiceError) {
    return json({ error: { code: e.code, message: e.message } }, e.status);
  }
  console.error(e);
  return json({ error: { code: "internal", message: "Something went wrong." } }, 500);
}

/**
 * CSRF guard for POSTs: the body must be JSON (a cross-site form cannot send that without a CORS preflight) and,
 * when the browser sends an Origin, it must be this site.
 */
export function assertJsonPost(request: Request): void {
  const type = request.headers.get("content-type") ?? "";
  if (!/^application\/json\s*(;|$)/i.test(type)) {
    throw new ServiceError("unsupported_media_type", "Send Content-Type: application/json.", 415);
  }
  const origin = request.headers.get("origin");
  if (origin !== null) {
    const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host");
    let originHost: string | null = null;
    try {
      originHost = new URL(origin).host;
    } catch {
      /* malformed Origin: rejected below */
    }
    if (!host || originHost !== host) throw new ServiceError("bad_origin", "Cross-origin requests are not allowed.", 403);
  }
}

export async function readJson(request: Request): Promise<Record<string, unknown>> {
  try {
    const body = await request.json();
    if (body && typeof body === "object" && !Array.isArray(body)) return body as Record<string, unknown>;
  } catch {
    /* fall through */
  }
  throw new ServiceError("bad_json", "The body must be a JSON object.", 400);
}

/** Parses a numeric id from a route param. */
export function idParam(raw: string): number {
  if (!/^\d+$/.test(raw) || raw.length > 15) throw new ServiceError("not_found", "Not found", 404);
  return Number(raw);
}
