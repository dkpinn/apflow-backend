-- Phase C6: Track AI token usage and cost per extraction.
-- Apply in Supabase SQL editor, then move this file to app/db/applied/.

ALTER TABLE public.invoices_extracted
  ADD COLUMN IF NOT EXISTS extraction_input_tokens  integer,
  ADD COLUMN IF NOT EXISTS extraction_output_tokens integer,
  ADD COLUMN IF NOT EXISTS extraction_model         text,
  ADD COLUMN IF NOT EXISTS extraction_cost_usd      numeric(10,6);

ALTER TABLE public.bank_statement_uploads
  ADD COLUMN IF NOT EXISTS extraction_input_tokens  integer,
  ADD COLUMN IF NOT EXISTS extraction_output_tokens integer,
  ADD COLUMN IF NOT EXISTS extraction_model         text,
  ADD COLUMN IF NOT EXISTS extraction_cost_usd      numeric(10,6);

COMMENT ON COLUMN public.invoices_extracted.extraction_input_tokens  IS 'Prompt token count from VLM extraction call.';
COMMENT ON COLUMN public.invoices_extracted.extraction_output_tokens IS 'Completion token count from VLM extraction call.';
COMMENT ON COLUMN public.invoices_extracted.extraction_model         IS 'Model name used for VLM extraction (e.g. gemini-2.5-flash).';
COMMENT ON COLUMN public.invoices_extracted.extraction_cost_usd      IS 'Estimated USD cost of VLM extraction based on published token rates.';

COMMENT ON COLUMN public.bank_statement_uploads.extraction_input_tokens  IS 'Prompt token count from VLM bank statement extraction.';
COMMENT ON COLUMN public.bank_statement_uploads.extraction_output_tokens IS 'Completion token count from VLM bank statement extraction.';
COMMENT ON COLUMN public.bank_statement_uploads.extraction_model         IS 'Model name used for VLM extraction.';
COMMENT ON COLUMN public.bank_statement_uploads.extraction_cost_usd      IS 'Estimated USD cost of VLM extraction.';
