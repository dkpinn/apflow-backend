-- Phase C20: Make the user organisations compatibility view respect caller RLS.
-- Apply in Supabase SQL editor, then move this file to app/db/applied/.

ALTER VIEW public.user_organisations
  SET (security_invoker = true);

REVOKE ALL PRIVILEGES ON TABLE public.user_organisations FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE public.user_organisations FROM anon;

GRANT SELECT ON TABLE public.user_organisations
  TO authenticated, service_role;

-- Security-invoker views check the caller's privileges on underlying relations.
-- organisation_users RLS continues to restrict authenticated callers by membership.
GRANT SELECT ON TABLE public.organisation_users
  TO authenticated, service_role;

COMMENT ON VIEW public.user_organisations IS
  'Active organisation memberships exposed with caller privileges and organisation_users RLS.';

SELECT 'user_organisations_security_invoker_phase_c20_ready' AS migration_note;
