-- Platform owners inherit protected owner membership in every organisation.
-- This migration does not seed any platform owner account.

CREATE TABLE IF NOT EXISTS public.platform_admin_users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  role TEXT NOT NULL DEFAULT 'owner' CHECK (role IN ('owner')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id)
);

ALTER TABLE public.platform_admin_users ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.organisation_users
  ADD COLUMN IF NOT EXISTS platform_managed BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS platform_previous_role public.organisation_role,
  ADD COLUMN IF NOT EXISTS platform_previous_status public.membership_status;

COMMENT ON COLUMN public.organisation_users.platform_managed IS
  'True while this membership is inherited from an active platform owner.';

DROP TRIGGER IF EXISTS platform_admin_users_set_updated_at
  ON public.platform_admin_users;
CREATE TRIGGER platform_admin_users_set_updated_at
  BEFORE UPDATE ON public.platform_admin_users
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE OR REPLACE FUNCTION public.sync_platform_owner_memberships(
  p_user_id UUID
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  is_active_owner BOOLEAN;
BEGIN
  SELECT EXISTS (
    SELECT 1
    FROM public.platform_admin_users
    WHERE user_id = p_user_id
      AND role = 'owner'
      AND status = 'active'
  )
  INTO is_active_owner;

  PERFORM set_config('app.platform_owner_membership_sync', 'on', true);

  IF is_active_owner THEN
    UPDATE public.organisation_users
    SET
      platform_previous_role = CASE
        WHEN platform_managed THEN platform_previous_role
        ELSE role
      END,
      platform_previous_status = CASE
        WHEN platform_managed THEN platform_previous_status
        ELSE status
      END,
      role = 'owner',
      status = 'active',
      platform_managed = true,
      accepted_at = coalesce(accepted_at, now()),
      updated_at = now()
    WHERE user_id = p_user_id;

    INSERT INTO public.organisation_users (
      organisation_id,
      user_id,
      role,
      status,
      accepted_at,
      platform_managed
    )
    SELECT
      organisation.id,
      p_user_id,
      'owner',
      'active',
      now(),
      true
    FROM public.organisations organisation
    WHERE NOT EXISTS (
      SELECT 1
      FROM public.organisation_users membership
      WHERE membership.organisation_id = organisation.id
        AND membership.user_id = p_user_id
    );
  ELSE
    DELETE FROM public.organisation_users
    WHERE user_id = p_user_id
      AND platform_managed
      AND platform_previous_role IS NULL;

    UPDATE public.organisation_users
    SET
      role = platform_previous_role,
      status = platform_previous_status,
      platform_managed = false,
      platform_previous_role = NULL,
      platform_previous_status = NULL,
      updated_at = now()
    WHERE user_id = p_user_id
      AND platform_managed
      AND platform_previous_role IS NOT NULL
      AND platform_previous_status IS NOT NULL;
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.sync_platform_owner_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  PERFORM public.sync_platform_owner_memberships(
    CASE WHEN TG_OP = 'DELETE' THEN OLD.user_id ELSE NEW.user_id END
  );
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE OR REPLACE FUNCTION public.add_platform_owners_to_new_organisation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  owner_id UUID;
BEGIN
  FOR owner_id IN
    SELECT user_id
    FROM public.platform_admin_users
    WHERE role = 'owner'
      AND status = 'active'
  LOOP
    PERFORM public.sync_platform_owner_memberships(owner_id);
  END LOOP;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.protect_platform_managed_membership()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
  internal_sync BOOLEAN :=
    coalesce(current_setting('app.platform_owner_membership_sync', true), 'off') = 'on';
BEGIN
  IF OLD.platform_managed AND NOT internal_sync THEN
    IF TG_OP = 'DELETE' THEN
      RAISE EXCEPTION 'Platform Owner membership cannot be deleted while platform access is active';
    END IF;

    IF NEW.user_id IS DISTINCT FROM OLD.user_id
       OR NEW.organisation_id IS DISTINCT FROM OLD.organisation_id
       OR NEW.role IS DISTINCT FROM OLD.role
       OR NEW.status IS DISTINCT FROM OLD.status
       OR NEW.platform_managed IS DISTINCT FROM OLD.platform_managed
       OR NEW.platform_previous_role IS DISTINCT FROM OLD.platform_previous_role
       OR NEW.platform_previous_status IS DISTINCT FROM OLD.platform_previous_status
    THEN
      RAISE EXCEPTION 'Platform Owner membership cannot be demoted or suspended while platform access is active';
    END IF;
  END IF;

  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

DROP TRIGGER IF EXISTS platform_admin_users_sync_memberships
  ON public.platform_admin_users;
CREATE TRIGGER platform_admin_users_sync_memberships
  AFTER INSERT OR UPDATE OF role, status OR DELETE
  ON public.platform_admin_users
  FOR EACH ROW EXECUTE FUNCTION public.sync_platform_owner_change();

DROP TRIGGER IF EXISTS organisations_add_platform_owners
  ON public.organisations;
CREATE TRIGGER organisations_add_platform_owners
  AFTER INSERT ON public.organisations
  FOR EACH ROW EXECUTE FUNCTION public.add_platform_owners_to_new_organisation();

DROP TRIGGER IF EXISTS organisation_users_protect_platform_managed
  ON public.organisation_users;
CREATE TRIGGER organisation_users_protect_platform_managed
  BEFORE UPDATE OR DELETE ON public.organisation_users
  FOR EACH ROW EXECUTE FUNCTION public.protect_platform_managed_membership();

DO $$
DECLARE
  owner_id UUID;
BEGIN
  FOR owner_id IN
    SELECT user_id
    FROM public.platform_admin_users
    WHERE role = 'owner'
      AND status = 'active'
  LOOP
    PERFORM public.sync_platform_owner_memberships(owner_id);
  END LOOP;
END;
$$;

REVOKE ALL PRIVILEGES ON TABLE public.platform_admin_users FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE public.platform_admin_users FROM anon;
REVOKE ALL PRIVILEGES ON TABLE public.platform_admin_users FROM authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.platform_admin_users
  TO service_role;

REVOKE ALL ON FUNCTION public.sync_platform_owner_memberships(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sync_platform_owner_change() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.add_platform_owners_to_new_organisation() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.protect_platform_managed_membership() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.sync_platform_owner_memberships(UUID)
  TO service_role;
