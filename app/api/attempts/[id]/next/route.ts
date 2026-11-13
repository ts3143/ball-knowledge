import type { NextRequest } from "next/server";
import { diveService, requireUserId } from "@/lib/dive/runtime";
import { assertJsonPost, errorResponse, idParam, json } from "@/lib/http";

export async function POST(request: NextRequest, ctx: { params: Promise<{ id: string }> }) {
  try {
    assertJsonPost(request);
    const { id } = await ctx.params;
    return json(await diveService().next(await requireUserId(), idParam(id)));
  } catch (e) {
    return errorResponse(e);
  }
}
