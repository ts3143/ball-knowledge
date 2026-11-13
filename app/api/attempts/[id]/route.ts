import type { NextRequest } from "next/server";
import { diveService, requireUserId } from "@/lib/dive/runtime";
import { errorResponse, idParam, json } from "@/lib/http";

// Resume state: the in-flight prompt with remaining_ms and the results so far (the summary once it is over).
export async function GET(_request: NextRequest, ctx: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await ctx.params;
    return json(await diveService().getState(await requireUserId(), idParam(id)));
  } catch (e) {
    return errorResponse(e);
  }
}
