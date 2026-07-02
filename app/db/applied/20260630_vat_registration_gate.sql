-- Review and apply with Supabase CLI. Do not move to app/db/applied until pushed.

ALTER TABLE public.organisations
  ADD COLUMN IF NOT EXISTS vat_registered boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS vat_registration_date date,
  ADD COLUMN IF NOT EXISTS vat_basis text NOT NULL DEFAULT 'not_applicable';

UPDATE public.organisations
   SET vat_registered = false
 WHERE vat_registered IS NULL;

UPDATE public.organisations
   SET vat_basis = 'not_applicable'
 WHERE vat_basis IS NULL OR trim(vat_basis) = '';

ALTER TABLE public.organisations
  DROP CONSTRAINT IF EXISTS organisations_vat_basis_chk;

ALTER TABLE public.organisations
  ADD CONSTRAINT organisations_vat_basis_chk
  CHECK (vat_basis IN ('not_applicable', 'invoice', 'payments'));

ALTER TABLE public.organisations
  DROP CONSTRAINT IF EXISTS organisations_vat_registration_date_chk;

ALTER TABLE public.organisations
  ADD CONSTRAINT organisations_vat_registration_date_chk
  CHECK (vat_registered IS NOT TRUE OR vat_registration_date IS NOT NULL)
  NOT VALID;

COMMENT ON COLUMN public.organisations.vat_registered IS
  'Authoritative flag for whether organisation VAT workflows are enabled.';

COMMENT ON COLUMN public.organisations.vat_registration_date IS
  'First transaction date from which VAT should be calculated, posted, and reported.';

COMMENT ON COLUMN public.organisations.vat_basis IS
  'VAT reporting basis. Use not_applicable unless vat_registered is true.';
