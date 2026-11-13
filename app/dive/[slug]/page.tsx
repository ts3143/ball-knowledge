import Link from "next/link";
import { notFound } from "next/navigation";
import { connection } from "next/server";
import { competitionBySlug, currentUserId, diveService } from "@/lib/dive/runtime";
import { SummaryView } from "../summary";
import { StartButton } from "./start-button";

export default async function DivePage({ params }: { params: Promise<{ slug: string }> }) {
  await connection();
  const { slug } = await params;
  const competition = competitionBySlug(slug);
  if (!competition) notFound();
  const userId = await currentUserId();
  const service = diveService();
  const view = await service.getCurrentDive(competition.qid, userId);

  let body;
  if (view.state === "no_dive") {
    body = <p>There is no dive for {competition.name} yet.</p>;
  } else if (view.state === "closed" || !view.dive) {
    body = <p>The last {competition.name} dive has closed. The next one opens soon.</p>;
  } else if (view.attempt && userId !== null && view.attempt.status !== "in_progress") {
    const state = await service.getState(userId, view.attempt.id);
    body = state.summary ? <SummaryView summary={state.summary} /> : <p>Dive finished.</p>;
  } else if (view.attempt) {
    body = (
      <>
        <p>You have a dive in progress.</p>
        <p><Link href={`/dive/${slug}/play`}><button>Resume</button></Link></p>
      </>
    );
  } else if (!view.dive.open) {
    body = <p>This dive has closed.</p>;
  } else {
    body = (
      <>
        <p>
          {view.dive.prompt_count} prompts, {view.dive.seconds_per_prompt} seconds each. Each one asks you to name
          something; type one answer. The rarer a correct answer, the more it is worth. You get one scored try per dive.
        </p>
        <StartButton diveId={view.dive.id} slug={slug} />
      </>
    );
  }
  return (
    <main>
      <p><Link href="/">Ball Knowledge</Link></p>
      <h1>{competition.name}</h1>
      {body}
    </main>
  );
}
