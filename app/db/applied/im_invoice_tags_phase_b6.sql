-- Phase B6: Add workflow tags array to invoices_extracted
-- Apply via Supabase SQL Editor or: supabase db push

ALTER TABLE invoices_extracted
  ADD COLUMN IF NOT EXISTS tags text[] DEFAULT '{}';
