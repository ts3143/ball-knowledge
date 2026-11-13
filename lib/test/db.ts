// Test-only database helpers (never imported by app code). Fixtures are inserted as the superuser-like `postgres`
// role through DATABASE_URL, the only place outside scripts/ and the pipeline that reads it; the service under test
// then runs as `bk_app` (set local role) so the real grants are exercised. Everything is rolled back.
import { existsSync } from "node:fs";
import postgres from "postgres";
import type { Sql } from "@/lib/db/client";

if (existsSync(".env")) process.loadEnvFile(".env");

class Rollback extends Error {}

let admin: postgres.Sql | null = null;

function adminDb(): postgres.Sql {
  const url = process.env.DATABASE_URL;
  if (!url) throw new Error("DATABASE_URL is not set (see .env.example); needed by the db tests");
  if (!admin) admin = postgres(url, { prepare: false, max: 1, onnotice: () => {} });
  return admin;
}

export async function closeAdmin(): Promise<void> {
  if (admin) await admin.end({ timeout: 2 });
  admin = null;
}

/** Runs `fn` in a transaction that is always rolled back. The handle starts with the fixture role (postgres). */
export async function inRolledBackTx(fn: (tx: Sql) => Promise<void>): Promise<void> {
  try {
    await adminDb().begin(async (tx) => {
      await fn(tx as unknown as Sql);
      throw new Rollback();
    });
  } catch (e) {
    if (!(e instanceof Rollback)) throw e;
  }
}

/** Switches the transaction to the app's least-privilege role. */
export async function asApp(tx: Sql): Promise<void> {
  await tx`set local role bk_app`;
}

/** Runs `fn` as the fixture role, then goes back to bk_app. */
export async function asAdmin<T>(tx: Sql, fn: () => Promise<T>): Promise<T> {
  await tx`reset role`;
  try {
    return await fn();
  } finally {
    await tx`set local role bk_app`;
  }
}
