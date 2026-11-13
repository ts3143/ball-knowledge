// npm run match -- <prompt_id> "<input>"
// Try the matcher on real data from the terminal. Read-only, through bk_app.
import { loadDotEnv } from "./env";
loadDotEnv();

async function main() {
  const [idArg, ...rest] = process.argv.slice(2);
  const input = rest.join(" ");
  const promptId = Number(idArg);
  if (!idArg || !Number.isInteger(promptId) || rest.length === 0) {
    console.error('usage: npm run match -- <prompt_id> "<input>"');
    process.exit(2);
  }
  const { db, closeDb } = await import("@/lib/db/client");
  const { matchingConfig } = await import("@/lib/config");
  const { buildIndex, matchAnswer, normalize } = await import("@/lib/matching");
  const { loadAnswerSet, loadOutside } = await import("@/lib/matching/load");

  const sql = db();
  try {
    const prompt = await loadAnswerSet(sql, promptId);
    if (!prompt) {
      console.error(`no prompt ${promptId}`);
      process.exit(1);
    }
    const cfg = matchingConfig();
    const index = buildIndex(prompt.answers, cfg);
    const norm = normalize(Array.from(input).slice(0, cfg.max_input_chars).join(""));
    const outside = await loadOutside(sql, prompt.answer_type, norm, index.ids);
    const r = matchAnswer(input, index, () => outside, cfg);

    console.log(`prompt ${prompt.id} [${prompt.template}, ${prompt.status}, ${prompt.answers.length} answers]`);
    console.log(`  ${prompt.text}`);
    console.log(`input:      ${JSON.stringify(input)}  ->  normalised ${JSON.stringify(r.input_norm)}${r.truncated ? " (truncated)" : ""}`);
    console.log(`verdict:    ${r.verdict}`);
    console.log(`match kind: ${r.match_kind ?? "-"}   edit distance: ${r.edit_distance ?? "-"}   ambiguous: ${r.ambiguous}`);
    console.log(`matched:    ${r.matched_name ?? "-"}${r.matched_entity_id ? ` (entity ${r.matched_entity_id}, key ${JSON.stringify(r.matched_key)})` : ""}`);
    console.log(`tier:       ${r.tier ?? "-"}   points: ${r.points}${r.hipster_pick ? "   hipster pick" : ""}`);
    if (r.candidates.length > 0) {
      console.log("candidates considered (best first):");
      for (const c of r.candidates.slice(0, 8)) {
        console.log(`  ${c.name} (entity ${c.entity_id})  key ${JSON.stringify(c.key)} [${c.key_class}]  distance ${c.distance}  share ${c.predicted_share.toFixed(4)}`);
      }
    }
    if (r.outside.length > 0) {
      console.log("real entities outside the set with this alias:");
      for (const o of r.outside.slice(0, 8)) console.log(`  ${o.canonical_name} (entity ${o.entity_id})`);
    }
  } finally {
    await closeDb();
  }
}

main().catch((e) => {
  console.error(e instanceof Error ? e.message : e);
  process.exit(1);
});
