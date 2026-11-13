import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const root = fileURLToPath(new URL(".", import.meta.url));

// `npm test` runs the unit tests; `npm run test:db` (BK_DB_TESTS=1) runs only the *.db.test.ts files.
const dbTests = process.env.BK_DB_TESTS === "1";

export default defineConfig({
  resolve: {
    alias: {
      "@": root,
      // server-only throws outside a Next server bundle; unit tests run in plain Node.
      "server-only": fileURLToPath(new URL("./lib/test/server-only-stub.ts", import.meta.url)),
    },
  },
  test: {
    include: dbTests ? ["**/*.db.test.ts"] : ["**/*.test.ts"],
    exclude: dbTests ? ["node_modules/**", ".next/**"] : ["node_modules/**", ".next/**", "**/*.db.test.ts"],
    passWithNoTests: dbTests,
  },
});
