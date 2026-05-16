import { supabase, FASTAPI_URL } from "@/integrations/supabase/client";

export type UploadKind = "invoice" | "statement";

const BUCKETS: Record<UploadKind, string> = {
  invoice: "invoices",
  statement: "statement-files",
};

function extractMissingColumn(message: string): string | null {
  // Matches both Postgres ("column \"X\" of relation ...") and
  // PostgREST schema-cache messages ("Could not find the 'X' column of '...'").
  const pg = message.match(/column\s+"?([a-zA-Z0-9_]+)"?\s+of relation/i);
  if (pg?.[1]) return pg[1];
  const prest = message.match(/find the '([a-zA-Z0-9_]+)' column/i);
  return prest?.[1] ?? null;
}

function stripNullish<T extends Record<string, unknown>>(value: T): T {
  return Object.fromEntries(Object.entries(value).filter(([, v]) => v !== undefined)) as T;
}

export function sanitizeFilename(name: string): string {
  return name.replace(/[^a-zA-Z0-9._-]+/g, "-").replace(/-+/g, "-");
}

export function storageBucket(kind: UploadKind) {
  return BUCKETS[kind];
}

export function buildStoragePath(kind: UploadKind, organisationId: string, filename: string) {
  const safe = sanitizeFilename(filename);
  return `${organisationId}/${kind}s/${Date.now()}-${safe}`;
}

export async function insertRowWithFallback(
  table: string,
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const current = stripNullish({ ...payload });

  for (;;) {
    const { data, error } = await supabase.from(table).insert(current).select("*").single();
    if (!error && data) return data as Record<string, unknown>;
    if (!error && !data) return current;
    if (!error) throw new Error(`Insert into ${table} failed without an error payload`);
    const missing = extractMissingColumn(error.message);
    if (missing && missing in current) {
      delete current[missing];
      continue;
    }
    // eslint-disable-next-line no-console
    console.error("[insertRowWithFallback] insert failed", {
      table,
      payload: current,
      error: {
        message: error.message,
        code: (error as { code?: string }).code,
        details: (error as { details?: string }).details,
        hint: (error as { hint?: string }).hint,
      },
    });
    throw error;
  }
}

export async function updateRowWithFallback(
  table: string,
  id: string,
  patch: Record<string, unknown>,
): Promise<void> {
  const current = stripNullish({ ...patch });

  for (;;) {
    const { error } = await supabase.from(table).update(current).eq("id", id);
    if (!error) return;
    const missing = extractMissingColumn(error.message);
    if (missing && missing in current) {
      delete current[missing];
      continue;
    }
    throw error;
  }
}

export async function insertManyWithFallback(table: string, rows: Record<string, unknown>[]) {
  if (rows.length === 0) return;
  const current = rows.map((row) => stripNullish({ ...row }));

  for (;;) {
    const { error } = await supabase.from(table).insert(current);
    if (!error) return;
    const missing = extractMissingColumn(error.message);
    if (missing) {
      let removed = false;
      for (const row of current) {
        if (missing in row) {
          delete row[missing];
          removed = true;
        }
      }
      if (removed) continue;
    }
    throw error;
  }
}

export async function ensureSupplier(
  organisationId: string,
  supplierName?: string | null,
): Promise<{ supplierId: string | null; supplierName: string | null }> {
  const normalised = supplierName?.trim() || null;
  if (!normalised) return { supplierId: null, supplierName: null };

  const { data: suppliers, error } = await supabase
    .from("suppliers")
    .select("*")
    .eq("organisation_id", organisationId)
    .limit(200);
  if (error) throw error;

  const match = ((suppliers ?? []) as Array<Record<string, unknown>>).find((row) => {
    const a = String(row.supplier_name ?? row.name ?? "").trim().toLowerCase();
    return a === normalised.toLowerCase();
  });
  if (match?.id) {
    return { supplierId: String(match.id), supplierName: normalised };
  }

  const created = await insertRowWithFallback("suppliers", {
    organisation_id: organisationId,
    supplier_name: normalised,
    name: normalised,
    status: "active",
  });

  return { supplierId: String(created.id), supplierName: normalised };
}

export function getFastApiUrl() {
  const base = FASTAPI_URL || "";
  if (!base) throw new Error("VITE_FASTAPI_URL is not configured");
  return base.replace(/\/$/, "");
}

export async function callExtraction(
  endpoint: "/api/invoices/extract" | "/api/extract/statement" | "/api/extract/invoice",
  body: Record<string, unknown>,
) {
  let response: Response;
  try {
    response = await fetch(`${getFastApiUrl()}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error("EXTRACT ERROR", err, { stage: "fetch", endpoint, body });
    throw err;
  }

  const rawText = await response.text().catch(() => "");

  if (!response.ok) {
    // eslint-disable-next-line no-console
    console.error("EXTRACT ERROR", {
      status: response.status,
      statusText: response.statusText,
      endpoint,
      requestBody: body,
      responseBody: rawText,
    });
    throw new Error(rawText || `Extraction failed (${response.status})`);
  }

  let parsed: Record<string, unknown> = {};
  try {
    parsed = rawText ? (JSON.parse(rawText) as Record<string, unknown>) : {};
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error("EXTRACT ERROR", "Failed to parse JSON response", err, {
      endpoint,
      responseBody: rawText,
    });
    throw err;
  }

  // eslint-disable-next-line no-console
  console.log("EXTRACT RESPONSE", parsed);
  return parsed;
}

export function pickObject(value: unknown, keys: string[]) {
  if (!value || typeof value !== "object") return null;
  const rec = value as Record<string, unknown>;
  for (const key of keys) {
    const v = rec[key];
    if (v && typeof v === "object") return v as Record<string, unknown>;
  }
  return null;
}

export function pickArray(value: unknown, keys: string[]) {
  if (!value || typeof value !== "object") return [] as Record<string, unknown>[];
  const rec = value as Record<string, unknown>;
  for (const key of keys) {
    const v = rec[key];
    if (Array.isArray(v)) return v as Record<string, unknown>[];
  }
  return [] as Record<string, unknown>[];
}

export function pickString(rec: Record<string, unknown> | null | undefined, keys: string[]) {
  for (const key of keys) {
    const value = rec?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}
