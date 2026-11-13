import type { NextRequest } from "next/server";
import { diveService, requireUserId } from "@/lib/dive/runtime";
import { assertJsonPost, errorResponse, idParam, json, readJson } from "@/lib/http";

export async function POST(request: NextRequest, ctx: { params: Promise<{ id: string }> }) {
  try {
    assertJsonPost(request);
    const { id } = await ctx.params;
    const body = await readJson(request);
    return json(await diveService().answer(await requireUserId(), idParam(id), body.position, body.text));
  } catch (e) {
    return errorResponse(e);
  }
}
