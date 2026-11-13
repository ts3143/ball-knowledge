import Link from "next/link";
import { connection } from "next/server";
import { competitions, currentUserId, diveService } from "@/lib/dive/runtime";

export default async function Home() {
  await connection(); // per request: depends on the clock and the session
  const userId = await currentUserId();
  const service = diveService();
  const live = [];
  for (const c of competitions()) {
    const view = await service.getCurrentDive(c.qid, userId);
    if (view.state === "available") live.push({ ...c, view });
  }
  return (
    <main>
      <h1>Ball Knowledge</h1>
      {live.length === 0 ? (
        <p>No dive is open right now. Check back soon.</p>
      ) : (
        <>
          <p>Today&apos;s dives:</p>
          <ul>
            {live.map((c) => (
              <li key={c.slug}>
                <Link href={`/dive/${c.slug}`}>{c.name}</Link>
                {c.view.attempt?.status === "finished" ? " (played)" : c.view.attempt ? " (in progress)" : ""}
              </li>
            ))}
          </ul>
        </>
      )}
    </main>
  );
}
