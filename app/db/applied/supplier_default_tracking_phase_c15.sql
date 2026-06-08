-- Phase C15: Supplier-wide default tracking for invoice allocations.
-- Apply in Supabase SQL editor, then move this file to app/db/applied/.

ALTER TABLE public.suppliers
  ADD COLUMN IF NOT EXISTS default_tracking JSONB NOT NULL DEFAULT '{}'::JSONB;

ALTER TABLE public.suppliers
  DROP CONSTRAINT IF EXISTS suppliers_default_tracking_object_check;

ALTER TABLE public.suppliers
  ADD CONSTRAINT suppliers_default_tracking_object_check
  CHECK (jsonb_typeof(default_tracking) = 'object');

COMMENT ON COLUMN public.suppliers.default_tracking IS
  'Default invoice allocation tracking as {tracking_dimension_id: tracking_value_id}. '
  'Line-specific and supplier allocation-rule tracking values take precedence.';

SELECT 'supplier_default_tracking_phase_c15_applied' AS migration_note;
