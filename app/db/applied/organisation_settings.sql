-- ============================================================
-- Organisation Settings — Phase 2
-- Adds a server-side guard preventing demotion or
-- suspension/revocation of the last active owner of an
-- organisation. Also enforces this on delete.
--
-- Apply manually in external Supabase. Idempotent.
-- ============================================================

create or replace function public.protect_last_owner()
returns trigger
language plpgsql
as $$
declare
  remaining_owners integer;
  target_org uuid;
begin
  -- Only act when the change would remove an active owner.
  if (tg_op = 'UPDATE') then
    target_org := old.organisation_id;
    if old.role = 'owner'::public.organisation_role
       and old.status = 'active'::public.membership_status
       and (
         new.role <> 'owner'::public.organisation_role
         or new.status <> 'active'::public.membership_status
       )
    then
      select count(*) into remaining_owners
      from public.organisation_users
      where organisation_id = target_org
        and role = 'owner'::public.organisation_role
        and status = 'active'::public.membership_status
        and id <> old.id;

      if remaining_owners = 0 then
        raise exception 'Cannot demote or suspend the last active owner of this organisation';
      end if;
    end if;
  elsif (tg_op = 'DELETE') then
    target_org := old.organisation_id;
    if old.role = 'owner'::public.organisation_role
       and old.status = 'active'::public.membership_status
    then
      select count(*) into remaining_owners
      from public.organisation_users
      where organisation_id = target_org
        and role = 'owner'::public.organisation_role
        and status = 'active'::public.membership_status
        and id <> old.id;

      if remaining_owners = 0 then
        raise exception 'Cannot remove the last active owner of this organisation';
      end if;
    end if;
  end if;

  if (tg_op = 'DELETE') then
    return old;
  end if;
  return new;
end;
$$;

drop trigger if exists organisation_users_protect_last_owner on public.organisation_users;
create trigger organisation_users_protect_last_owner
  before update or delete on public.organisation_users
  for each row execute function public.protect_last_owner();
