// The only place SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY are read (server-side only; never sent to the browser).
export function supabaseEnv(): { url: string; key: string } {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_PUBLISHABLE_KEY;
  if (!url || !key) throw new Error("SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY must be set (see .env.example)");
  return { url, key };
}

/** The session cookies are only ever read by the server (no browser Supabase client), so they are httpOnly. */
export const sessionCookieOptions = {
  httpOnly: true,
  sameSite: "lax" as const,
  secure: process.env.NODE_ENV === "production",
  path: "/",
};
