-- Each user's own API keys for Lead Finder (Hunter, Serper, Reoon, the AI writer).
-- Run this once in the Supabase dashboard: SQL Editor > New query > paste > Run.
--
-- The keys are encrypted by the backend before they are stored. Row level security is switched on with NO policies,
-- so the browser (anon key) can never read this table; only the backend (service role key) can.

create table if not exists public.prospect_api_keys (
  user_id       uuid        not null references public.users (id) on delete cascade,
  provider      text        not null check (provider in ('hunter', 'serper', 'reoon', 'ai')),
  key_encrypted text        not null,
  key_last4     text        not null,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  primary key (user_id, provider)
);

alter table public.prospect_api_keys enable row level security;

-- Make sure nothing is exposed to the public API roles
revoke all on public.prospect_api_keys from anon, authenticated;
