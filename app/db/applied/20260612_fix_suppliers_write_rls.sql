-- Fix: suppliers table has no working UPDATE/INSERT RLS policy under the
-- current organisation_users/organisation_role membership model. The legacy
-- "Writers can update suppliers" / "Writers can insert suppliers" policies
-- rely on can_write_org() -> has_role(), which checks the old user_roles
-- table and now matches nobody for current users, silently updating 0 rows
-- (no error is raised, so saves from the Supplier page appear to succeed but
-- never persist).
--
-- Add a policy matching the pattern already used by other tables (e.g.
-- accounts_write, bank_accounts_write_accountants, gl_journals_write_accountants).
-- Apply via the Supabase SQL Editor.

CREATE POLICY "suppliers_write_accountants" ON "public"."suppliers"
  TO "authenticated"
  USING ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]))
  WITH CHECK ("public"."has_org_role"("organisation_id", ARRAY['owner'::"public"."organisation_role", 'admin'::"public"."organisation_role", 'accountant'::"public"."organisation_role"]));
