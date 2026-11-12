-- Test mode: while on, every email goes to the user's own mailbox instead of the real person.
-- Run this once in the Supabase dashboard: SQL Editor > New query > paste > Run.

alter table public.prospect_mailboxes
  add column if not exists test_mode boolean not null default true;
