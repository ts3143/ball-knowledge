import { notFound, redirect } from "next/navigation";
import { connection } from "next/server";
import { competitionBySlug, currentUserId, diveService } from "@/lib/dive/runtime";
import { Play } from "./play";

export default async function PlayPage({ params }: { params: Promise<{ slug: string }> }) {
  await connection();
  const { slug } = await params;
  const competition = competitionBySlug(slug);
  if (!competition) notFound();
  const userId = await currentUserId();
  const view = await diveService().getCurrentDive(competition.qid, userId);
  // Nothing to play: go back to the dive page, which shows start, summary or closed.
  if (!view.attempt || view.attempt.status !== "in_progress") redirect(`/dive/${slug}`);
  return (
    <main>
      <h1>{competition.name}</h1>
      <Play attemptId={view.attempt.id} slug={slug} />
    </main>
  );
}
