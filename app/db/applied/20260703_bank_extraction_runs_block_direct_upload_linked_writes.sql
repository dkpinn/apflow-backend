-- Review and apply with Supabase CLI. Do not move to app/db/applied until pushed.
--
-- bank_statement_extraction_runs is now part of the accounting trust gate.
-- Authenticated users may still save ad hoc/offline benchmark runs, but they
-- must not directly create or alter upload-linked benchmark evidence. Backend
-- service-role routes that rerun the parser against the source document remain
-- responsible for writing trusted upload-linked runs.

DROP POLICY IF EXISTS "bank_extraction_runs_write_accountants"
  ON public.bank_statement_extraction_runs;

DROP POLICY IF EXISTS "bank_extraction_runs_insert_ad_hoc"
  ON public.bank_statement_extraction_runs;
CREATE POLICY "bank_extraction_runs_insert_ad_hoc"
  ON public.bank_statement_extraction_runs
  FOR INSERT TO authenticated
  WITH CHECK (
    bank_statement_upload_id IS NULL
    AND (
      (organisation_id IS NULL AND public.is_platform_admin())
      OR (
        organisation_id IS NOT NULL
        AND public.has_org_role(
          organisation_id,
          ARRAY['owner','admin','accountant']::public.organisation_role[]
        )
      )
    )
  );

DROP POLICY IF EXISTS "bank_extraction_runs_update_ad_hoc"
  ON public.bank_statement_extraction_runs;
CREATE POLICY "bank_extraction_runs_update_ad_hoc"
  ON public.bank_statement_extraction_runs
  FOR UPDATE TO authenticated
  USING (
    bank_statement_upload_id IS NULL
    AND (
      (organisation_id IS NULL AND public.is_platform_admin())
      OR (
        organisation_id IS NOT NULL
        AND public.has_org_role(
          organisation_id,
          ARRAY['owner','admin','accountant']::public.organisation_role[]
        )
      )
    )
  )
  WITH CHECK (
    bank_statement_upload_id IS NULL
    AND (
      (organisation_id IS NULL AND public.is_platform_admin())
      OR (
        organisation_id IS NOT NULL
        AND public.has_org_role(
          organisation_id,
          ARRAY['owner','admin','accountant']::public.organisation_role[]
        )
      )
    )
  );

DROP POLICY IF EXISTS "bank_extraction_runs_delete_ad_hoc"
  ON public.bank_statement_extraction_runs;
CREATE POLICY "bank_extraction_runs_delete_ad_hoc"
  ON public.bank_statement_extraction_runs
  FOR DELETE TO authenticated
  USING (
    bank_statement_upload_id IS NULL
    AND (
      (organisation_id IS NULL AND public.is_platform_admin())
      OR (
        organisation_id IS NOT NULL
        AND public.has_org_role(
          organisation_id,
          ARRAY['owner','admin','accountant']::public.organisation_role[]
        )
      )
    )
  );
