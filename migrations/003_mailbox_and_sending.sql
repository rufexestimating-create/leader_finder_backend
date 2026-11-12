-- Each user's own mailbox (e.g. a Hostinger address) that ReachAgent sends from, and the send result on each person.
-- Run this once in the Supabase dashboard: SQL Editor > New query > paste > Run.
--
-- The password is encrypted by the backend before it is stored. Row level security is on with NO policies, so only the
-- backend (service role key) can read the table.

create table if not exists public.prospect_mailboxes (
  user_id            uuid        primary key references public.users (id) on delete cascade,
  email              text        not null,
  from_name          text,
  smtp_host          text        not null,
  smtp_port          integer     not null,
  imap_host          text,
  imap_port          integer,
  password_encrypted text        not null,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

alter table public.prospect_mailboxes enable row level security;
revoke all on public.prospect_mailboxes from anon, authenticated;

alter table public.prospect_contacts
  add column if not exists sent_at    timestamptz,
  add column if not exists send_error text,
  add column if not exists message_id text;
