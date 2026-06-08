-- Phase b14: add 'needs_split' to the parse_status enum on invoices_raw
-- This value is set when Gemini detects multiple separate documents on a single uploaded page.
-- The invoice is paused for manual crop-splitting before extraction can produce accurate results.

ALTER TYPE parse_status ADD VALUE IF NOT EXISTS 'needs_split';
