-- Add 'needs_review' to bank_statement_uploads extraction_status constraint.
-- Required because extract_bank_upload sets this status when validation flags
-- a statement that extracted successfully but needs manual review.
ALTER TABLE public.bank_statement_uploads
    DROP CONSTRAINT bank_statement_uploads_extraction_status_check;

ALTER TABLE public.bank_statement_uploads
    ADD CONSTRAINT bank_statement_uploads_extraction_status_check
    CHECK (extraction_status IN ('uploaded','processing','extracted','failed','needs_review'));
