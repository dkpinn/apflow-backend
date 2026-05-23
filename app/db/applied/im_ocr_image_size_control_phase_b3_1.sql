-- ============================================================
-- OCR Image Size Control Phase B3.1
-- No schema changes required.
--
-- Backend-only patch:
-- - capped PDF-to-image rendering DPI/dimensions before OCR
-- - capped image dimensions before Tesseract
-- - stores OCR/image quality in existing document_pages columns
-- ============================================================
select 'ocr_image_size_control_phase_b3_1_no_schema_change' as migration_note;
