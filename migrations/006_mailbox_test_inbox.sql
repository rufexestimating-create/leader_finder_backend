-- Test inbox: while test mode is on, every email goes here (e.g. your Gmail) instead of the real person.
-- Empty means the mailbox's own address. Run this once in the Supabase dashboard: SQL Editor > New query > paste > Run.

alter table public.prospect_mailboxes
  add column if not exists test_to text;
