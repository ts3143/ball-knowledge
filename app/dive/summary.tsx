import type { Summary } from "@/lib/dive/service-types";

export function SummaryView({ summary }: { summary: Summary }) {
  return (
    <section>
      <h2>Your dive</h2>
      {summary.status === "abandoned" && <p>This dive was not finished in time; the prompts you did not reach count 0.</p>}
      <table>
        <thead>
          <tr><th>#</th><th>Prompt</th><th>Your answer</th><th>Counted as</th><th>Tier</th><th>Points</th></tr>
        </thead>
        <tbody>
          {summary.results.map((r) => (
            <tr key={r.position}>
              <td>{r.position}</td>
              <td>{r.text}</td>
              <td>{r.answer && r.answer.trim() !== "" ? r.answer : "—"}</td>
              <td>{r.matched_name ?? "—"}</td>
              <td>{r.verdict === "counted" ? `${r.tier_name}${r.hipster_pick ? " (Hipster Pick)" : ""}` : "—"}</td>
              <td>{r.points}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p><strong>Total: {summary.total} / {summary.max}</strong></p>
    </section>
  );
}
