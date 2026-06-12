ALTER TABLE public.organisations
  ADD COLUMN IF NOT EXISTS wca_number text;
