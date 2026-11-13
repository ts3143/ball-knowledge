import type { NextRequest } from "next/server";
import { competitionBySlug, currentUserId, diveService } from "@/lib/dive/runtime";
import { ServiceError } from "@/lib/dive/errors";
import { errorResponse, json } from "@/lib/http";

// Dive metadata and the caller's attempt, if any. No prompt text, and it never creates a session.
export async function GET(_request: NextRequest, ctx: { params: Promise<{ slug: string }> }) {
  try {
    const { slug } = await ctx.params;
    const competition = competitionBySlug(slug);
    if (!competition) throw new ServiceError("not_found", "No such competition", 404);
    const view = await diveService().getCurrentDive(competition.qid, await currentUserId());
    return json({ competition: { slug, name: competition.name }, ...view });
  } catch (e) {
    return errorResponse(e);
  }
}
