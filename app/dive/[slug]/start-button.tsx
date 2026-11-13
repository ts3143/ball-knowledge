"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

export function StartButton({ diveId, slug }: { diveId: number; slug: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/attempts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dive_id: diveId }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body?.error?.message ?? "Could not start the dive.");
      router.push(`/dive/${slug}/play`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the dive.");
      setBusy(false);
    }
  }

  return (
    <p>
      <button onClick={start} disabled={busy}>Start the dive</button>
      {error && <span className="error"> {error}</span>}
    </p>
  );
}
