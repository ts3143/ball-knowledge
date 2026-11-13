import "server-only";
import postgres from "postgres";

// The only place the app reads APP_DATABASE_URL (connects as the least-privilege role bk_app).
// The pipeline's DATABASE_URL (a superuser) must never be read by app code.

export type Sql = postgres.Sql;

let client: Sql | null = null;

export function appDatabaseUrl(): string {
  const url = process.env.APP_DATABASE_URL;
  if (!url) throw new Error("APP_DATABASE_URL is not set (see .env.example; run `npm run db:app-role`)");
  return url;
}

/** One connection pool per process. prepare:false keeps it compatible with a transaction-mode pooler later. */
export function db(): Sql {
  if (!client) client = postgres(appDatabaseUrl(), { prepare: false, max: 5 });
  return client;
}

export async function closeDb(): Promise<void> {
  if (client) await client.end({ timeout: 2 });
  client = null;
}
