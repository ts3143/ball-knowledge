import "server-only";
import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";
import { sessionCookieOptions, supabaseEnv } from "./env";

// Server-side session handling for route handlers and server components. Sessions live in httpOnly cookies that
// proxy.ts refreshes; the identity is always verified with auth.getClaims() (never trusted from the cookie).

async function serverClient() {
  const store = await cookies();
  const { url, key } = supabaseEnv();
  return createServerClient(url, key, {
    cookieOptions: sessionCookieOptions,
    cookies: {
      getAll: () => store.getAll(),
      setAll(list) {
        // Server components cannot set cookies (proxy.ts refreshes the session for them); route handlers can.
        try {
          for (const { name, value, options } of list) store.set(name, value, options);
        } catch {
          /* read-only context */
        }
      },
    },
  });
}

/** The verified Supabase user id (the claims' `sub`) of the request's session, or null when there is none. */
export async function getAuthUserId(): Promise<string | null> {
  const supabase = await serverClient();
  const { data, error } = await supabase.auth.getClaims();
  if (error || !data?.claims?.sub) return null;
  return data.claims.sub;
}

/** The session's user id; with no session, signs in anonymously (sets the cookies on this response). Route handlers only. */
export async function ensureAuthUserId(): Promise<string> {
  const existing = await getAuthUserId();
  if (existing) return existing;
  const supabase = await serverClient();
  const { data, error } = await supabase.auth.signInAnonymously();
  if (error || !data.user) throw new Error(`anonymous sign-in failed: ${error?.message ?? "no user"}`);
  return data.user.id;
}
