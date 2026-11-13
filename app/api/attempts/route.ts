import type { NextRequest } from "next/server";
import { ensureCurrentUser, diveService } from "@/lib/dive/runtime";
import { ServiceError } from "@/lib/dive/errors";
import { assertJsonPost, errorResponse, json, readJson } from "@/lib/http";

// Ensures a session (anonymous sign-in when there is none) and a users row, then creates the attempt or returns the
// existing one so the player resumes.
export async function POST(request: NextRequest) {
  try {
    assertJsonPost(request);
    const body = await readJson(request);
    const diveId = body.dive_id;
    if (typeof diveId !== "number" || !Number.isInteger(diveId) || diveId <= 0) {
      throw new ServiceError("bad_dive_id", "dive_id must be a positive integer", 400);
    }
    const userId = await ensureCurrentUser();
    const { attempt, created } = await diveService().createOrResumeAttempt(userId, diveId);
    return json({ attempt, created }, created ? 201 : 200);
  } catch (e) {
    return errorResponse(e);
  }
}
