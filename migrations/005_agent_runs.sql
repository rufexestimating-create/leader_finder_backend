-- ReachAgent's automatic runs: find companies on Google Maps, read their websites, find emails and send.
-- Run this once in the Supabase dashboard: SQL Editor > New query > paste > Run.

create table if not exists public.prospect_agent_runs (
  id           uuid        primary key default gen_random_uuid(),
  user_id      uuid        not null references public.users (id) on delete cascade,
  mode         text        not null default 'fast',
  filters      jsonb       not null,
  status       text        not null default 'queued',
  steps        jsonb       not null default '[]'::jsonb,
  counts       jsonb       not null default '{}'::jsonb,
  processed    jsonb       not null default '[]'::jsonb,
  error        text,
  next_send_at timestamptz,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  finished_at  timestamptz
);
create index if not exists prospect_agent_runs_user on public.prospect_agent_runs (user_id, created_at desc);
create index if not exists prospect_agent_runs_status on public.prospect_agent_runs (status);

alter table public.prospect_agent_runs enable row level security;
revoke all on public.prospect_agent_runs from anon, authenticated;

alter table public.prospect_companies
  add column if not exists agent_run_id uuid references public.prospect_agent_runs (id) on delete set null;

alter table public.prospect_settings
  add column if not exists agent_subject  text,
  add column if not exists agent_message  text,
  add column if not exists emails_per_day integer;
