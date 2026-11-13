"use client";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { describeResult } from "@/lib/dive/describe";
import type { AttemptState, NextResult, PlayerResult } from "@/lib/dive/service-types";

interface Prompt {
  position: number;
  count: number;
  text: string;
}

async function api<T>(path: string, method: "GET" | "POST", body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: method === "POST" ? { "Content-Type": "application/json" } : undefined,
    body: body === undefined ? (method === "POST" ? "{}" : undefined) : JSON.stringify(body),
    cache: "no-store",
  });
  const data = await res.json();
  if (!res.ok) throw Object.assign(new Error(data?.error?.message ?? "Request failed"), { code: data?.error?.code });
  return data as T;
}

export function Play({ attemptId, slug }: { attemptId: number; slug: string }) {
  const router = useRouter();
  const [prompt, setPrompt] = useState<Prompt | null>(null);
  const [result, setResult] = useState<PlayerResult | null>(null);
  const [remaining, setRemaining] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const deadline = useRef(0); // performance.now() value at which the countdown reaches 0
  const submitted = useRef(false);
  const base = `/api/attempts/${attemptId}`;

  const showPrompt = useCallback((p: Prompt, remainingMs: number) => {
    deadline.current = performance.now() + remainingMs;
    submitted.current = false;
    setResult(null);
    setRemaining(remainingMs);
    setPrompt(p);
    setTimeout(() => inputRef.current?.focus(), 0);
  }, []);

  const finish = useCallback(async () => {
    await api(`${base}/finish`, "POST");
    router.replace(`/dive/${slug}`);
    router.refresh();
  }, [base, router, slug]);

  const next = useCallback(async () => {
    setError(null);
    try {
      const n = await api<NextResult>(`${base}/next`, "POST");
      if ("done" in n) return await finish();
      showPrompt({ position: n.position, count: n.prompt_count, text: n.text }, n.remaining_ms);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
    }
  }, [base, finish, showPrompt]);

  const load = useCallback(async () => {
    setError(null);
    try {
      const s = await api<AttemptState>(base, "GET");
      if (s.summary) {
        router.replace(`/dive/${slug}`);
        router.refresh();
        return;
      }
      if (!s.current) return await next();
      const p = { position: s.current.position, count: s.current.prompt_count, text: s.current.text };
      if (s.current.result) {
        setPrompt(p);
        setResult(s.current.result);
        submitted.current = true;
      } else {
        showPrompt(p, s.current.remaining_ms);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
    }
  }, [base, next, router, showPrompt, slug]);

  useEffect(() => {
    void load();
  }, [load]);

  const submit = useCallback(
    async (text: string) => {
      if (!prompt || submitted.current) return;
      submitted.current = true;
      try {
        setResult(await api<PlayerResult>(`${base}/answer`, "POST", { position: prompt.position, text }));
      } catch (e) {
        setError(e instanceof Error ? e.message : "Something went wrong.");
      }
    },
    [base, prompt],
  );

  // The countdown is driven by the server's remaining_ms plus performance.now(); at 0 it submits what is typed.
  useEffect(() => {
    if (!prompt || result) return;
    const id = setInterval(() => {
      const left = Math.max(0, deadline.current - performance.now());
      setRemaining(left);
      if (left <= 0) void submit(inputRef.current?.value ?? "");
    }, 100);
    return () => clearInterval(id);
  }, [prompt, result, submit]);

  if (error) {
    return (
      <div>
        <p className="error">{error}</p>
        <button onClick={() => void load()}>Try again</button>
      </div>
    );
  }
  if (!prompt) return <p>Loading&hellip;</p>;

  const isLast = prompt.position === prompt.count;
  return (
    <div>
      <p>{prompt.position} of {prompt.count}</p>
      <h2>{prompt.text}</h2>
      {result ? (
        <div>
          <p role="status"><strong>{describeResult(result)}</strong></p>
          <button onClick={() => void next()} autoFocus>{isLast ? "See results" : "Next prompt"}</button>
        </div>
      ) : (
        <form onSubmit={(e) => { e.preventDefault(); void submit(inputRef.current?.value ?? ""); }}>
          <p className="timer">{Math.ceil(remaining / 1000)}s</p>
          <input ref={inputRef} type="text" name="answer" autoFocus autoComplete="off" autoCapitalize="off"
                 spellCheck={false} maxLength={100} aria-label="Your answer" />
          <p>
            <button type="submit">Submit</button>
            <button type="button" onClick={() => void submit("")}>Pass</button>
          </p>
        </form>
      )}
    </div>
  );
}
