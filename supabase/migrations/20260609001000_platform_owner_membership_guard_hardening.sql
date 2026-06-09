-- Keep the internal sync bypass transaction-scoped and allow organisation
-- deletion to cascade inherited memberships.

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

  PERFORM set_config('app.platform_owner_membership_sync', 'off', true);
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
      IF NOT EXISTS (
        SELECT 1
        FROM public.organisations
        WHERE id = OLD.organisation_id
      ) THEN
        RETURN OLD;
      END IF;

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

REVOKE ALL ON FUNCTION public.sync_platform_owner_memberships(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.protect_platform_managed_membership() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.sync_platform_owner_memberships(UUID)
  TO service_role;
