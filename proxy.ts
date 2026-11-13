import type { NextRequest } from "next/server";
import { updateSession } from "@/lib/auth/proxy";

// Next 16's replacement for middleware: keeps the Supabase session fresh for pages and API routes.
export async function proxy(request: NextRequest) {
  return updateSession(request);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
