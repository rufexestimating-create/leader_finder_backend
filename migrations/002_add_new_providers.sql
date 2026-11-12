-- Extend prospect_api_keys to allow the four new providers: Apollo, Google Places, OpenAI, PDL.
-- Run this once in the Supabase dashboard: SQL Editor > New query > paste > Run.

alter table public.prospect_api_keys
  drop constraint if exists prospect_api_keys_provider_check;

alter table public.prospect_api_keys
  add constraint prospect_api_keys_provider_check
  check (provider in ('hunter', 'serper', 'reoon', 'ai', 'apollo', 'gplaces', 'openai', 'pdl'));
