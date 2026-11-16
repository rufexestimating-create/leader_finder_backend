-- Sending server override (e.g. Brevo): when set, emails go out through this SMTP server instead of the mailbox's own.
-- The mailbox's email and IMAP stay as before. Run once in Supabase: SQL Editor > New query > paste > Run.

alter table public.prospect_mailboxes
  add column if not exists send_host text,
  add column if not exists send_port integer,
  add column if not exists send_user text,
  add column if not exists send_pass_encrypted text;
