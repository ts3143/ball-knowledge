import { existsSync } from "node:fs";

/** Loads .env from the working directory (Node's built-in loader); existing env vars win. */
export function loadDotEnv(): void {
  if (existsSync(".env")) process.loadEnvFile(".env");
}
