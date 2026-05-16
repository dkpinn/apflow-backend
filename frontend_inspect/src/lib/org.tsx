import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { supabase } from "@/integrations/supabase/client";
import { useAuth } from "./auth";

export type OrganisationRole =
  | "owner"
  | "admin"
  | "accountant"
  | "reviewer"
  | "viewer"
  | "client";

export type Organisation = {
  id: string;
  name: string;
  legal_name?: string | null;
  registration_number?: string | null;
  vat_number?: string | null;
  tax_number?: string | null;
  country?: string | null;
  base_currency?: string | null;
  financial_year_end?: string | null;
  status?: string | null;
  [key: string]: unknown;
};

export type Membership = {
  organisation_id: string;
  role: OrganisationRole;
};

type OrgCtx = {
  organisations: Organisation[];
  memberships: Membership[];
  currentOrgId: string | null;
  currentOrg: Organisation | null;
  currentRole: OrganisationRole | null;
  setCurrentOrgId: (id: string) => void;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  hasRole: (roles: OrganisationRole | OrganisationRole[]) => boolean;
};

const Ctx = createContext<OrgCtx | undefined>(undefined);

const STORAGE_KEY = "apflow.currentOrgId";

export function OrgProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const [organisations, setOrganisations] = useState<Organisation[]>([]);
  const [memberships, setMemberships] = useState<Membership[]>([]);
  const [currentOrgId, setCurrentOrgIdState] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const setCurrentOrgId = (id: string) => {
    setCurrentOrgIdState(id);
    try {
      window.localStorage.setItem(STORAGE_KEY, id);
    } catch {
      /* ignore */
    }
  };

  const load = async () => {
    if (!user) {
      setOrganisations([]);
      setMemberships([]);
      setCurrentOrgIdState(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      // Active memberships for the current user.
      const { data: mems, error: mErr } = await supabase
        .from("organisation_users")
        .select("organisation_id, role, status")
        .eq("user_id", user.id)
        .eq("status", "active");

      if (mErr) throw mErr;

      const activeMems: Membership[] = (mems ?? []).map((m) => ({
        organisation_id: m.organisation_id as string,
        role: m.role as OrganisationRole,
      }));
      setMemberships(activeMems);

      let orgs: Organisation[] = [];
      if (activeMems.length > 0) {
        const ids = activeMems.map((m) => m.organisation_id);
        const { data: orgRows, error: oErr } = await supabase
          .from("organisations")
          .select("*")
          .in("id", ids);
        if (oErr) throw oErr;
        orgs = (orgRows ?? []) as Organisation[];
      }
      setOrganisations(orgs);

      const stored =
        (typeof window !== "undefined" && window.localStorage.getItem(STORAGE_KEY)) || null;
      const initial =
        (stored && orgs.find((o) => o.id === stored)?.id) || orgs[0]?.id || null;
      setCurrentOrgIdState(initial);
    } catch (e) {
      const message = e instanceof Error ? e.message : "Failed to load organisations";
      setError(message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id]);

  const currentOrg = useMemo(
    () => organisations.find((o) => o.id === currentOrgId) ?? null,
    [organisations, currentOrgId],
  );

  const currentRole = useMemo<OrganisationRole | null>(
    () => memberships.find((m) => m.organisation_id === currentOrgId)?.role ?? null,
    [memberships, currentOrgId],
  );

  const hasRole = (roles: OrganisationRole | OrganisationRole[]) => {
    if (!currentRole) return false;
    const list = Array.isArray(roles) ? roles : [roles];
    return list.includes(currentRole);
  };

  return (
    <Ctx.Provider
      value={{
        organisations,
        memberships,
        currentOrgId,
        currentOrg,
        currentRole,
        setCurrentOrgId,
        loading,
        error,
        refresh: load,
        hasRole,
      }}
    >
      {children}
    </Ctx.Provider>
  );
}

export function useOrg() {
  const v = useContext(Ctx);
  if (!v) throw new Error("useOrg must be used within OrgProvider");
  return v;
}
