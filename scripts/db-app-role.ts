// npm run db:app-role
// Gives the bk_app role (created NOLOGIN by migration 20261118000005) a login password taken from APP_DATABASE_URL.
//
// ALTER ROLE needs an admin connection, so this script, and ONLY this script, reads the pipeline's DATABASE_URL.
// App code (lib/, app/) must never read DATABASE_URL; it connects as bk_app through APP_DATABASE_URL.
import postgres from "postgres";
import { loadDotEnv } from "./env";
loadDotEnv();

async function main() {
  const appUrl = process.env.APP_DATABASE_URL;
  if (!appUrl) {
    console.error("APP_DATABASE_URL is not set. Add a line like this to .env (choose your own password):");
    console.error("  APP_DATABASE_URL=postgresql://bk_app:<password>@127.0.0.1:54322/postgres");
    process.exit(1);
  }
  const adminUrl = process.env.DATABASE_URL; // admin connection, this script only
  if (!adminUrl) {
    console.error("DATABASE_URL (the pipeline's admin connection) is not set in .env.");
    process.exit(1);
  }
  const u = new URL(appUrl);
  const password = decodeURIComponent(u.password);
  if (u.username !== "bk_app" || !password || password.includes("<")) {
    console.error("APP_DATABASE_URL must use user bk_app and a real password (not the <password> placeholder).");
    process.exit(1);
  }

  const admin = postgres(adminUrl, { max: 1 });
  try {
    const [stmt] = await admin<{ s: string }[]>`select format('alter role bk_app login password %L', ${password}::text) as s`;
    await admin.unsafe((stmt as { s: string }).s);
  } finally {
    await admin.end();
  }

  // Prove the login works and the role is least-privilege.
  const app = postgres(appUrl, { max: 1, prepare: false });
  try {
    const [r] = await app<{ who: string; n: string }[]>`select current_user as who, (select count(*) from prompts)::text as n`;
    console.log(`bk_app can log in (current_user = ${r?.who}) and read prompts (${r?.n} rows).`);
    const denied = await app`select 1 from facts limit 1`.then(() => false, () => true);
    console.log(`facts is ${denied ? "denied (as intended)" : "READABLE, check the grants"}.`);
  } finally {
    await app.end();
  }
}

main().catch((e) => {
  console.error(e instanceof Error ? e.message : e);
  process.exit(1);
});
