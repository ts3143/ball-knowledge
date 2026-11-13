import "server-only";
import { ensureAuthUserId, getAuthUserId } from "@/lib/auth";
import { loadTunables } from "@/lib/config";
import { db } from "@/lib/db/client";
import { ServiceError } from "./errors";
import { createDiveService } from "./service";

/** The service over the app's pool and the real clock. */
export const diveService = () => createDiveService(db());

/** The player of this request, or null with no session (read-only: never creates anything). */
export async function currentUserId(): Promise<number | null> {
  const authId = await getAuthUserId();
  return authId ? diveService().findUser(authId) : null;
}

/** The player of this request; with no session, signs in anonymously and creates the users row. */
export async function ensureCurrentUser(): Promise<number> {
  return diveService().ensureUser(await ensureAuthUserId());
}

/** A signed-in player is required (everything but starting an attempt). */
export async function requireUserId(): Promise<number> {
  const id = await currentUserId();
  if (id === null) throw new ServiceError("no_session", "No session. Start a dive first.", 401);
  return id;
}

export interface CompetitionConfig {
  slug: string;
  qid: string;
  name: string;
}

export function competitions(): CompetitionConfig[] {
  const c = loadTunables().competitions as Record<string, { qid: string; name: string }>;
  return Object.entries(c).map(([slug, v]) => ({ slug, qid: v.qid, name: v.name }));
}

export function competitionBySlug(slug: string): CompetitionConfig | null {
  return competitions().find((c) => c.slug === slug) ?? null;
}
