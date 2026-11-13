-- Step 5 (chunk 1): T's review of prompts. A gate_passed prompt becomes 'approved' (it may be scheduled) or
-- 'rejected' (it never ships). generate leaves both alone. reviewed_at and review_note record when and why.
-- `python -m prompts approve|reject|review` is the only writer.
--
-- Security: no new tables. prompts keeps its RLS and revokes (20261112000000_prompts.sql), which cover the new
-- columns too.

alter table prompts
  drop constraint prompts_status_check,
  add constraint prompts_status_check
    check (status in ('candidate', 'gate_failed', 'gate_passed', 'approved', 'rejected')),
  add column reviewed_at timestamptz,
  add column review_note text;
