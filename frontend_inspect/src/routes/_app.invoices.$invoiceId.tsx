import { createFileRoute, Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  RotateCcw,
  Save,
  Send,
  AlertTriangle,
  FileWarning,
  Pencil,
  Plus,
  Trash2,
  ZoomIn,
  ZoomOut,
  Maximize2,
  ChevronLeft,
  ChevronRight,
  Download,
} from "lucide-react";
import { supabase, FASTAPI_URL } from "@/integrations/supabase/client";
import { useOrg } from "@/lib/org";
import { useAuth } from "@/lib/auth";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Separator } from "@/components/ui/separator";
import { StatusBadge } from "@/components/app/StatusBadge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";

export const Route = createFileRoute("/_app/invoices/$invoiceId")({
  validateSearch: (s: Record<string, unknown>): { admin?: boolean; from?: string } => ({
    admin: s.admin === true || s.admin === "1" || s.admin === "true" ? true : undefined,
    from: typeof s.from === "string" ? s.from : undefined,
  }),
  component: InvoiceDetail,
});

type AnyRec = Record<string, unknown>;

type LineItemsReextractDiagnostic = {
  line_items_found_count: number | null;
  line_items_inserted_count: number | null;
  line_items_insert_error: string | null;
  line_items_total: number | null;
  invoice_total: number | null;
  line_items_match_invoice_total: boolean | null;
};

function InvoiceDetail() {
  const { invoiceId } = Route.useParams();
  const search = useSearch({ from: "/_app/invoices/$invoiceId" });
  const navigate = useNavigate();
  const { currentOrgId } = useOrg();
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const [saving, setSaving] = useState(false);
  const [reextracting, setReextracting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<null | "extracted" | "all">(null);
  const [editOpen, setEditOpen] = useState(false);
  const [populatingSupplier, setPopulatingSupplier] = useState(false);
  const [confirmPopulate, setConfirmPopulate] = useState<null | "fill" | "overwrite">(null);
  const [linkSupplierOpen, setLinkSupplierOpen] = useState(false);
  const [supplierMutating, setSupplierMutating] = useState(false);
  const [lineItemsReextractDiagnostic, setLineItemsReextractDiagnostic] =
    useState<LineItemsReextractDiagnostic | null>(() =>
      readStoredLineItemsDiagnostic(invoiceId),
    );

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["invoice_extracted", invoiceId, currentOrgId],
    queryFn: async () => {
      // The route param may be either an invoices_extracted.id OR an
      // invoices_raw.id (when navigating directly from the upload queue).
      // Resolve in this order:
      //   1) invoices_extracted.id = invoiceId
      //   2) invoices_extracted.invoice_raw_id = invoiceId (latest by created_at)
      // eslint-disable-next-line no-console
      console.log("REVIEW QUERY FILTER", {
        invoiceId,
        organisation_id: currentOrgId,
        tries: ["id", "invoice_raw_id"],
      });

      // Try 1: by extracted id (with embedded supplier)
      let q = supabase
        .from("invoices_extracted")
        .select("*, supplier:suppliers(*)")
        .eq("id", invoiceId);
      if (currentOrgId) q = q.eq("organisation_id", currentOrgId);
      const primary = await q.maybeSingle();
      if (!primary.error && primary.data) {
        // eslint-disable-next-line no-console
        console.log("REVIEW RESULT", { matchedBy: "id", row: primary.data });
        return primary.data as AnyRec;
      }

      // Try 1b: same id without embedded supplier
      let q2 = supabase
        .from("invoices_extracted")
        .select("*")
        .eq("id", invoiceId);
      if (currentOrgId) q2 = q2.eq("organisation_id", currentOrgId);
      const inv = await q2.maybeSingle();
      if (inv.error) throw inv.error;

      let row: AnyRec | null = (inv.data as AnyRec | null) ?? null;
      let matchedBy: "id" | "invoice_raw_id" = "id";

      // Try 2: by invoice_raw_id (latest)
      if (!row) {
        let q3 = supabase
          .from("invoices_extracted")
          .select("*")
          .eq("invoice_raw_id", invoiceId)
          .order("created_at", { ascending: false })
          .limit(1);
        if (currentOrgId) q3 = q3.eq("organisation_id", currentOrgId);
        const byRaw = await q3.maybeSingle();
        if (byRaw.error && byRaw.error.code !== "PGRST116") throw byRaw.error;
        if (byRaw.data) {
          row = byRaw.data as AnyRec;
          matchedBy = "invoice_raw_id";
        }
      }

      if (!row) {
        // eslint-disable-next-line no-console
        console.log("REVIEW RESULT", { matchedBy: null, row: null });
        return null;
      }

      const supplierId = row.supplier_id as string | undefined;
      let supplier: AnyRec | null = null;
      if (supplierId) {
        const sup = await supabase
          .from("suppliers")
          .select("*")
          .eq("id", supplierId)
          .maybeSingle();
        supplier = (sup.data ?? null) as AnyRec | null;
      }
      const result = { ...row, supplier } as AnyRec;
      // eslint-disable-next-line no-console
      console.log("REVIEW RESULT", { matchedBy, row: result });
      return result;
    },
  });

  // Fetch the linked invoices_raw record (for file_path preview + parsed fallback fields).
  const rawId = (data?.invoice_raw_id as string | undefined) ?? null;
  const { data: rawRecord, refetch: refetchRawRecord } = useQuery({
    queryKey: ["invoices_raw_for_review", rawId],
    enabled: !!rawId,
    queryFn: async () => {
      const { data: r, error: e } = await supabase
        .from("invoices_raw")
        .select("*")
        .eq("id", rawId as string)
        .maybeSingle();
      if (e) {
        // eslint-disable-next-line no-console
        console.error("RAW RECORD fetch error", e);
        return null;
      }
      return (r as AnyRec | null) ?? null;
    },
  });

  const { data: documentPages, refetch: refetchDocumentPages } = useQuery({
    queryKey: ["document_pages_for_review", rawId],
    enabled: !!rawId,
    queryFn: async () => {
      const { data: pages, error: pagesError } = await supabase
        .from("document_pages")
        .select("*")
        .eq("invoice_raw_id", rawId as string)
        .order("page_number", { ascending: true })
        .limit(100);
      if (pagesError) {
        // eslint-disable-next-line no-console
        console.warn("DOCUMENT PAGES fetch error", pagesError);
        return [] as AnyRec[];
      }
      return (pages ?? []) as AnyRec[];
    },
  });

  // Parsed JSON may live on either invoices_extracted or invoices_raw, depending on backend.
  const parsed: AnyRec =
    (data && typeof data.parsed === "object" && data.parsed
      ? (data.parsed as AnyRec)
      : null) ||
    (rawRecord && typeof rawRecord.parsed === "object" && rawRecord.parsed
      ? (rawRecord.parsed as AnyRec)
      : {}) ||
    {};

  // eslint-disable-next-line no-console
  console.log("REVIEW DATA (EXTRACTED)", data);
  // eslint-disable-next-line no-console
  console.log("REVIEW DATA (PARSED)", parsed);
  // eslint-disable-next-line no-console
  console.log("RAW RECORD (for preview)", rawRecord);

  const ref =
    pickStr(data, ["invoice_number", "reference", "number"]) ||
    pickStr(parsed, ["invoice_number", "reference", "number"]) ||
    invoiceId.slice(0, 8);
  const supplier = (data?.supplier as AnyRec | null | undefined) ?? null;
  // Supplier resolution: extracted.supplier_name_extracted → suppliers join → parsed.supplier_name → "Unknown supplier"
  const supplierName =
    pickStr(data, ["supplier_name_extracted"]) ||
    pickStr(supplier, ["supplier_name", "name", "trading_name"]) ||
    pickStr(data, ["supplier_name", "supplier", "vendor"]) ||
    pickStr(parsed, ["supplier_name", "supplier", "vendor"]) ||
    "Unknown supplier";
  const status =
    pickStr(data, ["review_status", "parse_status", "status", "state"]) ||
    "captured";
  const subtotal =
    pickNum(data, ["subtotal", "net_amount", "sub_total"]) ??
    pickNum(parsed, ["subtotal", "net_amount", "sub_total"]);
  // VAT: invoices_extracted.tax_amount (mapped from backend vat_amount) → parsed.vat_amount
  const tax =
    pickNum(data, ["tax_amount", "vat_amount", "tax", "vat"]) ??
    pickNum(parsed, ["vat_amount", "tax_amount", "tax", "vat"]);
  // Total: invoices_extracted.total_amount → parsed.total_amount → subtotal + tax
  let total =
    pickNum(data, ["total_amount", "total", "amount", "grand_total"]) ??
    pickNum(parsed, ["total_amount", "total", "amount", "grand_total"]);
  if (total == null && (subtotal != null || tax != null)) {
    total = (subtotal ?? 0) + (tax ?? 0);
  }
  // eslint-disable-next-line no-console
  console.log("TOTAL FIELD VALUE", (data as AnyRec | null)?.total_amount);
  const currency =
    pickStr(data, ["currency", "currency_code"]) ||
    pickStr(parsed, ["currency", "currency_code"]) ||
    "USD";
  const invoiceDate =
    pickStr(data, ["invoice_date", "issue_date", "date"]) ||
    pickStr(parsed, ["invoice_date", "issue_date", "date"]);
  const dueDate =
    pickStr(data, ["due_date", "payment_due_date"]) ||
    pickStr(parsed, ["due_date", "payment_due_date"]);
  const confidence = pickNum(data, [
    "confidence_score",
    "confidence",
    "parse_confidence",
  ]);
  const notes = pickStr(data, ["notes", "review_notes", "comment"]);

  // Banking — use the actual *_extracted columns from invoices_extracted
  const extractedBankAcct = pickStr(data, [
    "bank_account_number_extracted",
    "bank_account_number",
    "account_number",
    "iban",
    "bank_account",
  ]);
  const extractedBankSort = pickStr(data, [
    "bank_branch_code_extracted",
    "bank_sort_code",
    "sort_code",
    "routing_number",
    "bsb",
  ]);
  const extractedBankName = pickStr(data, [
    "bank_name_extracted",
    "bank_name",
    "bank",
  ]);
  const extractedBankAcctName = pickStr(data, ["bank_account_name_extracted"]);
  const extractedBankSwift = pickStr(data, ["bank_swift_code_extracted"]);

  // Overrides take precedence over extracted values for validation & payment.
  const overrideBankAcct = pickStr(data, ["override_bank_account_number"]);
  const overrideBankSort = pickStr(data, ["override_sort_code"]);
  const overrideBankName = pickStr(data, ["override_bank_name"]);
  const effectiveBankAcct = overrideBankAcct ?? extractedBankAcct;
  const effectiveBankSort = overrideBankSort ?? extractedBankSort;
  const effectiveBankName = overrideBankName ?? extractedBankName;
  const hasOverrides = !!(overrideBankAcct || overrideBankSort || overrideBankName);

  const supplierBankAcct = pickStr(supplier, [
    "bank_account_number",
    "account_number",
    "iban",
    "bank_account",
  ]);
  const supplierBankSort = pickStr(supplier, [
    "bank_branch_code",
    "bank_sort_code",
    "sort_code",
    "routing_number",
    "bsb",
  ]);
  const supplierBankName = pickStr(supplier, ["bank_name", "bank"]);
  const supplierBankAcctName = pickStr(supplier, ["bank_account_name"]);
  const supplierBankSwift = pickStr(supplier, ["bank_swift_code"]);
  const supplierHasAnyBank = !!(
    supplierBankAcct ||
    supplierBankName ||
    supplierBankSort ||
    supplierBankAcctName ||
    supplierBankSwift
  );
  const supplierBankMatchesExtracted =
    supplierHasAnyBank &&
    (!extractedBankAcct ||
      !supplierBankAcct ||
      normaliseAcct(extractedBankAcct) === normaliseAcct(supplierBankAcct)) &&
    (!extractedBankSort ||
      !supplierBankSort ||
      normaliseAcct(extractedBankSort) === normaliseAcct(supplierBankSort)) &&
    (!extractedBankName ||
      !supplierBankName ||
      extractedBankName.trim().toLowerCase() === supplierBankName.trim().toLowerCase());
  const bankingChecks = [
    {
      label: "account number",
      invoice: effectiveBankAcct,
      master: supplierBankAcct,
      compare: (a: string, b: string) => normaliseAcct(a) === normaliseAcct(b),
    },
    {
      label: "bank name",
      invoice: effectiveBankName,
      master: supplierBankName,
      compare: (a: string, b: string) =>
        a.trim().toLowerCase() === b.trim().toLowerCase(),
    },
    {
      label: "sort / routing",
      invoice: effectiveBankSort,
      master: supplierBankSort,
      compare: (a: string, b: string) => normaliseAcct(a) === normaliseAcct(b),
    },
  ].map((c) => {
    let status: "match" | "mismatch" | "missing-invoice" | "missing-master" | "missing-both";
    if (!c.invoice && !c.master) status = "missing-both";
    else if (!c.invoice && c.master) status = "missing-invoice";
    else if (c.invoice && !c.master) status = "missing-master";
    else if (c.compare(c.invoice as string, c.master as string)) status = "match";
    else status = "mismatch";
    return { ...c, status };
  });

  const acctCheck = bankingChecks[0];
  const bankingMismatch = acctCheck.status === "mismatch";
  const bankingMissingOnInvoice = acctCheck.status === "missing-invoice";
  const bankingFullyMatches =
    bankingChecks.every((c) => c.status === "match" || c.status === "missing-both") &&
    bankingChecks.some((c) => c.status === "match");
  const bankingHasAnyIssue = bankingChecks.some(
    (c) => c.status === "mismatch" || c.status === "missing-invoice",
  );
  const extractedSupplierProfile = pickSupplierProfile(parsed, data);
  const extractedSupplierCreatePayload = pickSupplierCreatePayload(parsed, data);
  const supplierDraftInitial = useMemo(
    () =>
      supplierDraftFromPayload(
        extractedSupplierCreatePayload ??
          buildSupplierCreatePayload({
            profile: extractedSupplierProfile,
            row: data,
            parsed,
            organisationId: currentOrgId ?? "",
            fallbackCurrency: currency,
            fallbackBanking: {
              bank_account_name: extractedBankAcctName,
              bank_name: extractedBankName,
              bank_account_number: extractedBankAcct,
              bank_branch_code: extractedBankSort,
              bank_swift_code: extractedBankSwift,
            },
          }),
      ),
    [
      currentOrgId,
      currency,
      data,
      extractedSupplierCreatePayload,
      extractedBankAcct,
      extractedBankAcctName,
      extractedBankName,
      extractedBankSort,
      extractedBankSwift,
      extractedSupplierProfile,
      parsed,
    ],
  );
  const [supplierDraft, setSupplierDraft] = useState<Record<string, string>>(supplierDraftInitial);
  const supplierDraftFields = supplierDraftDisplayFields(supplierDraft);
  const supplierIdentityDraftKeys = new Set([
    "supplier_name",
    "supplier_code",
    "vat_number",
    "company_registration_number",
  ]);
  const supplierIdentityFields = [
    {
      key: "supplier_name",
      label: "Supplier name",
      value: supplierDraft.supplier_name ?? "",
      editable: true,
      changed: Boolean(pickStr(data, ["issuer_name_extracted"]) && pickStr(data, ["issuer_name_extracted"]) !== supplierDraft.supplier_name),
    },
    {
      key: "vat_number",
      label: "VAT number",
      value: supplierDraft.vat_number ?? "",
      editable: true,
    },
    {
      key: "supplier_code",
      label: "Customer code",
      value: supplierDraft.supplier_code ?? "",
      editable: true,
      changed: Boolean(rawRecord && /receipt|cash|card/i.test(JSON.stringify(rawRecord)) && supplierDraft.supplier_code),
    },
    {
      key: "company_registration_number",
      label: "Company registration",
      value: supplierDraft.company_registration_number ?? "",
      editable: true,
    },
    {
      key: "issuer_name_extracted",
      label: "Issuer name",
      value: pickStr(data, ["issuer_name_extracted"]) ?? "",
      editable: false,
      changed: Boolean(
        pickStr(data, ["issuer_name_extracted"]) &&
          supplierDraft.supplier_name &&
          pickStr(data, ["issuer_name_extracted"]) !== supplierDraft.supplier_name,
      ),
    },
    {
      key: "recipient_name_extracted",
      label: "Recipient name",
      value: pickStr(data, ["recipient_name_extracted"]) ?? "",
      editable: false,
    },
  ];
  const supplierAdditionalDraftFields = supplierDraftFields.filter(
    (field) => !supplierIdentityDraftKeys.has(field.key),
  );

  useEffect(() => {
    setSupplierDraft(supplierDraftInitial);
  }, [supplierDraftInitial]);

  useEffect(() => {
    setLineItemsReextractDiagnostic(readStoredLineItemsDiagnostic(invoiceId));
  }, [invoiceId]);

  async function handleMarkReviewed() {
    setSaving(true);
    try {
      const { error } = await supabase
        .from("invoices_extracted")
        .update({ review_status: "reviewed" })
        .eq("id", invoiceId);
      if (error) throw error;
      toast.success("Invoice marked as reviewed");
      await refetch();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function handleReextract() {
    if (!rawId) {
      toast.error("Missing invoice_raw_id — cannot re-extract");
      return;
    }
    if (!FASTAPI_URL) {
      toast.error("VITE_FASTAPI_URL is not configured");
      return;
    }
    if (!currentOrgId) {
      toast.error("Select an organisation first");
      return;
    }
    setReextracting(true);
    setLineItemsReextractDiagnostic(null);
    storeLineItemsDiagnostic((data?.id as string | undefined) ?? invoiceId, null);
    try {
      const res = await fetch(`${FASTAPI_URL.replace(/\/$/, "")}/api/invoices/re-extract`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          invoice_raw_id: rawId,
          organisation_id: currentOrgId,
        }),
      });
      const text = await res.text().catch(() => "");
      if (!res.ok) throw new Error(text || `Re-extract failed (${res.status})`);
      let parsed: Record<string, unknown> = {};
      try {
        parsed = text ? (JSON.parse(text) as Record<string, unknown>) : {};
      } catch {
        // ignore
      }
      const diagnostic = parseLineItemsReextractDiagnostic(parsed);
      toast.success("Invoice re-extracted");
      const extractedId = parsed.extracted_invoice_id;
      const targetInvoiceId =
        typeof extractedId === "string" && extractedId
          ? extractedId
          : ((data?.id as string | undefined) ?? invoiceId);
      setLineItemsReextractDiagnostic(diagnostic);
      storeLineItemsDiagnostic(targetInvoiceId, diagnostic);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["invoice_extracted"] }),
        queryClient.invalidateQueries({ queryKey: ["invoices_raw_for_review", rawId] }),
        queryClient.invalidateQueries({ queryKey: ["document_pages_for_review", rawId] }),
        queryClient.invalidateQueries({ queryKey: ["invoice_line_items"] }),
        queryClient.invalidateQueries({ queryKey: ["invoice_raw_audit_events", rawId] }),
      ]);
      if (typeof extractedId === "string" && extractedId && extractedId !== invoiceId) {
        navigate({ to: "/invoices/$invoiceId", params: { invoiceId: extractedId } });
      } else {
        await Promise.all([
          refetch(),
          refetchRawRecord(),
          refetchDocumentPages(),
          queryClient.refetchQueries({
            queryKey: ["invoice_line_items", targetInvoiceId],
            type: "active",
          }),
          refetchAudit(),
        ]);
      }
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setReextracting(false);
    }
  }

  async function handleDelete(mode: "extracted" | "all") {
    setDeleting(true);
    try {
      // 1. Delete the extracted row
      const { error: delExtErr } = await supabase
        .from("invoices_extracted")
        .delete()
        .eq("id", (data?.id as string) ?? invoiceId);
      if (delExtErr) throw delExtErr;

      if (mode === "all" && rawId) {
        // 2. Delete storage object (best effort)
        const filePath = pickStr(rawRecord, ["file_path"]);
        if (filePath) {
          let p = filePath.trim().replace(/^\/+/, "");
          if (p.startsWith("invoices/")) p = p.slice("invoices/".length);
          const { error: storageErr } = await supabase.storage.from("invoices").remove([p]);
          if (storageErr) console.warn("Storage delete failed", storageErr);
        }
        // 3. Delete invoices_raw
        const { error: delRawErr } = await supabase
          .from("invoices_raw")
          .delete()
          .eq("id", rawId);
        if (delRawErr) throw delRawErr;
      }

      toast.success(mode === "all" ? "Upload and extracted invoice deleted" : "Invoice deleted");
      const target = search.from === "upload" ? "/invoices/upload" : "/invoices";
      navigate({ to: target });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setDeleting(false);
      setConfirmDelete(null);
    }
  }

  const { data: auditRows, refetch: refetchAudit } = useQuery({
    queryKey: ["invoice_raw_audit_events", rawId],
    enabled: Boolean(rawId && FASTAPI_URL),
    queryFn: async () => {
      const res = await fetch(
        `${FASTAPI_URL.replace(/\/$/, "")}/api/invoices/raw/${rawId}/audit-events`,
      );
      const text = await res.text().catch(() => "");
      if (!res.ok) throw new Error(text || `Audit events failed (${res.status})`);
      let parsed: unknown = [];
      try {
        parsed = text ? JSON.parse(text) : [];
      } catch {
        return [] as AnyRec[];
      }
      if (Array.isArray(parsed)) return parsed as AnyRec[];
      if (parsed && typeof parsed === "object") {
        const record = parsed as AnyRec;
        if (Array.isArray(record.events)) return record.events as AnyRec[];
        if (Array.isArray(record.audit_events)) return record.audit_events as AnyRec[];
        if (Array.isArray(record.data)) return record.data as AnyRec[];
      }
      return [] as AnyRec[];
    },
  });

  async function handleSaveOverrides(values: {
    override_bank_account_number: string | null;
    override_bank_name: string | null;
    override_sort_code: string | null;
  }) {
    setSaving(true);
    try {
      const payload = {
        override_bank_account_number: values.override_bank_account_number,
        override_bank_name: values.override_bank_name,
        override_sort_code: values.override_sort_code,
        overrides_updated_at: new Date().toISOString(),
        overrides_updated_by: user?.id ?? null,
      };
      const { error: updateErr } = await supabase
        .from("invoices_extracted")
        .update(payload)
        .eq("id", invoiceId);
      if (updateErr) throw updateErr;

      // Best-effort audit insert. Will silently no-op if the table is not present.
      const auditPayload = {
        invoice_id: invoiceId,
        organisation_id: currentOrgId,
        actor_id: user?.id ?? null,
        actor_email: user?.email ?? null,
        event_type: "banking_override_saved",
        before: {
          bank_account_number: extractedBankAcct,
          bank_name: extractedBankName,
          sort_code: extractedBankSort,
        },
        after: {
          override_bank_account_number: values.override_bank_account_number,
          override_bank_name: values.override_bank_name,
          override_sort_code: values.override_sort_code,
        },
      };
      await supabase.from("invoice_audit_log").insert(auditPayload);

      toast.success("Banking overrides saved");
      setEditOpen(false);
      await Promise.all([refetch(), refetchAudit()]);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function handlePopulateSupplierBanking(mode: "fill" | "overwrite") {
    const supplierId = data?.supplier_id as string | undefined;
    if (!supplierId) {
      toast.error("Link or create supplier before updating supplier master");
      return;
    }
    setPopulatingSupplier(true);
    try {
      const inferredCountry = inferCountryFromCurrency(currency);
      const payload: Record<string, unknown> = {
        bank_account_name: extractedBankAcctName ?? null,
        bank_name: extractedBankName ?? null,
        bank_account_number: extractedBankAcct ?? null,
        bank_branch_code: extractedBankSort ?? null,
        bank_swift_code: extractedBankSwift ?? null,
        bank_verified: false,
        bank_details_source: "invoice_extraction",
        bank_details_last_updated_at: new Date().toISOString(),
      };
      if (inferredCountry) payload.bank_country = inferredCountry;
      // Strip null entries so we don't blow away existing values when extracted is empty for that field.
      Object.keys(payload).forEach((k) => {
        if (payload[k] === null) delete payload[k];
      });
      const { error: updErr } = await supabase
        .from("suppliers")
        .update(payload)
        .eq("id", supplierId);
      if (updErr) throw updErr;
      toast.success("Supplier banking details updated");
      setConfirmPopulate(null);
      await refetch();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setPopulatingSupplier(false);
    }
  }

  async function handleLinkSupplier(supplierId: string) {
    setSupplierMutating(true);
    try {
      const { error } = await supabase
        .from("invoices_extracted")
        .update({ supplier_id: supplierId })
        .eq("id", (data?.id as string) ?? invoiceId);
      if (error) throw error;
      toast.success("Supplier linked");
      setLinkSupplierOpen(false);
      await refetch();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSupplierMutating(false);
    }
  }

  async function handleCreateSupplier() {
    if (!currentOrgId) {
      toast.error("Select an organisation first");
      return;
    }

    const payload = supplierPayloadFromDraft(supplierDraft, currentOrgId);

    if (!String(payload.supplier_name ?? "").trim()) {
      toast.error("Supplier name is required");
      return;
    }

    setSupplierMutating(true);
    try {
      const { data: inserted, error } = await supabase
        .from("suppliers")
        .insert(payload)
        .select("id")
        .single();
      if (error) throw error;
      const newId = (inserted as { id: string }).id;
      const { error: linkErr } = await supabase
        .from("invoices_extracted")
        .update({ supplier_id: newId })
        .eq("id", (data?.id as string) ?? invoiceId);
      if (linkErr) throw linkErr;
      toast.success("Supplier created and linked");
      await refetch();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSupplierMutating(false);
    }
  }

  function setSupplierDraftField(key: string, value: string) {
    setSupplierDraft((prev) => ({ ...prev, [key]: value }));
  }

  return (
    <>
      <div className="mb-5">
        <Link to="/invoices">
          <Button variant="ghost" size="sm" className="-ml-2 gap-2">
            <ArrowLeft className="h-4 w-4" /> Back to invoices
          </Button>
        </Link>
      </div>

      <div className="mb-7 flex flex-wrap items-end justify-between gap-4 border-b pb-5">
        <div className="min-w-0">
          <div className="flex items-center gap-3">
            <h1 className="text-[26px] font-semibold leading-tight tracking-tight">
              Invoice {ref}
            </h1>
            <StatusBadge status={status} />
            {bankingMismatch && (
              <span className="inline-flex items-center gap-1.5 rounded-md border border-destructive/30 bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive">
                <AlertTriangle className="h-3.5 w-3.5" /> Banking mismatch
              </span>
            )}
            {!bankingMismatch && bankingMissingOnInvoice && (
              <span className="inline-flex items-center gap-1.5 rounded-md border border-destructive/30 bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive">
                <AlertTriangle className="h-3.5 w-3.5" /> Missing invoice banking
              </span>
            )}
          </div>
          <p className="mt-1.5 text-sm text-muted-foreground">
            {supplierName ? `${supplierName} · ` : ""}
            <span className="font-mono text-xs">{invoiceId}</span>
          </p>
        </div>
        {total != null && (
          <div className="text-right">
            <div className="text-xs uppercase tracking-wider text-muted-foreground">
              Total
            </div>
            <div className="text-2xl font-semibold tabular-nums">
              {fmtMoney(total, currency)}
            </div>
          </div>
        )}
      </div>

      {isLoading ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : error ? (
        <div className="text-sm text-destructive">{(error as Error).message}</div>
      ) : !data ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 p-6">
          <div className="text-base font-semibold text-destructive">
            No extracted data found for this upload
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            No extracted invoice was found for id{" "}
            <span className="font-mono">{invoiceId}</span> in your current
            organisation.
          </p>
        </div>
      ) : (
        <div className="space-y-5">
          {/* Compact action toolbar */}
          <Card className="card-elevated">
            <CardContent className="flex flex-wrap items-center gap-2 p-3">
              <Button
                size="sm"
                variant="outline"
                disabled={reextracting || !rawId}
                onClick={handleReextract}
                className="gap-2"
              >
                <RotateCcw className="h-4 w-4" />
                {reextracting ? "Re-extracting…" : "Re-extract"}
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={saving}
                onClick={() => toast.info("Save coming soon — wire to FastAPI")}
                className="gap-2"
              >
                <Save className="h-4 w-4" /> Save
              </Button>
              <Button
                size="sm"
                disabled={saving}
                onClick={handleMarkReviewed}
                className="gap-2"
              >
                <CheckCircle2 className="h-4 w-4" /> Mark reviewed
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled
                className="gap-2"
              >
                <Send className="h-4 w-4" /> Approve invoice
              </Button>
              <div className="ml-auto flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={deleting}
                  onClick={() => setConfirmDelete("extracted")}
                  className="gap-2"
                >
                  <Trash2 className="h-4 w-4" /> Delete
                </Button>
                {(search.admin || search.from === "upload") && (
                  <Button
                    size="sm"
                    variant="destructive"
                    disabled={deleting}
                    onClick={() => setConfirmDelete("all")}
                    className="gap-2"
                  >
                    <Trash2 className="h-4 w-4" /> Delete upload and extracted
                  </Button>
                )}
              </div>
            </CardContent>
          </Card>

          {/* Two-column review area: Document Preview (wider) | Tabs */}
          <div className="grid gap-5 lg:grid-cols-5">
            <Card className="card-elevated lg:col-span-3">
              <CardContent className="pt-6">
                <DocumentPreview
                  row={data}
                  raw={rawRecord ?? null}
                  pages={documentPages ?? []}
                />
              </CardContent>
            </Card>

            <div className="lg:col-span-2">
              <Tabs defaultValue="extracted">
                <TabsList>
                  <TabsTrigger value="extracted">Extracted data</TabsTrigger>
                  <TabsTrigger value="banking">
                    Banking
                    {bankingHasAnyIssue && (
                      <span className="ml-1.5 inline-block h-1.5 w-1.5 rounded-full bg-destructive" />
                    )}
                  </TabsTrigger>
                  <TabsTrigger value="supplier">
                    Supplier
                    {data?.supplier_id ? (
                      <span className="ml-1.5 inline-block h-2 w-2 rounded-full bg-success" />
                    ) : (
                      <span className="ml-1.5 inline-flex h-4 w-4 items-center justify-center rounded-full bg-destructive text-[10px] font-bold leading-none text-destructive-foreground">
                        !
                      </span>
                    )}
                  </TabsTrigger>
                  <TabsTrigger value="audit">Audit</TabsTrigger>
                </TabsList>

                <TabsContent value="extracted" className="mt-4 space-y-5">
                  <EditableExtractedCard
                    invoiceId={invoiceId}
                    data={data}
                    currency={currency}
                    onSaved={() => refetch()}
                  />
                </TabsContent>

              <TabsContent value="banking" className="mt-4">
                <Card className="card-elevated">
                  <CardHeader className="flex flex-row items-center justify-between gap-3 space-y-0">
                    <CardTitle className="text-base">Banking details</CardTitle>
                    <Button
                      size="sm"
                      variant="outline"
                      className="gap-2"
                      onClick={() => setEditOpen(true)}
                    >
                      <Pencil className="h-3.5 w-3.5" /> Edit Banking Details
                    </Button>
                  </CardHeader>
                  <CardContent className="space-y-5">
                    {!extractedBankAcct &&
                      !extractedBankName &&
                      !extractedBankSort &&
                      !extractedBankAcctName &&
                      !extractedBankSwift &&
                      !overrideBankAcct &&
                      !overrideBankName &&
                      !overrideBankSort && (
                        <p className="text-sm text-muted-foreground">
                          No banking details extracted yet.
                        </p>
                      )}
                    {(extractedBankAcctName || extractedBankSwift) && (
                      <div className="grid gap-3 sm:grid-cols-2">
                        {extractedBankAcctName && (
                          <Field
                            label="Account name (extracted)"
                            value={extractedBankAcctName}
                            readonly
                          />
                        )}
                        {extractedBankSwift && (
                          <Field
                            label="SWIFT / BIC (extracted)"
                            value={extractedBankSwift}
                            mono
                            readonly
                          />
                        )}
                      </div>
                    )}
                    {hasOverrides && (
                      <BankingAlert
                        tone="warning"
                        title="Manual overrides are in effect for this invoice."
                        detail="Validation and payment will use override values instead of the originally extracted data."
                      />
                    )}
                    <div className="space-y-2">
                      {bankingChecks.map((c) => {
                        if (c.status === "mismatch") {
                          return (
                            <BankingAlert
                              key={c.label}
                              tone="destructive"
                              title={`Banking mismatch: Invoice ${c.label} does not match supplier master.`}
                              detail={`Invoice: ${c.invoice} · Supplier master: ${c.master}`}
                            />
                          );
                        }
                        if (c.status === "missing-invoice") {
                          return (
                            <BankingAlert
                              key={c.label}
                              tone="destructive"
                              title={`No ${c.label} found on invoice. Supplier master details will be used.`}
                              detail={`Supplier master ${c.label}: ${c.master}`}
                            />
                          );
                        }
                        if (c.status === "missing-master") {
                          return (
                            <BankingAlert
                              key={c.label}
                              tone="warning"
                              title={`No ${c.label} on supplier master. Cannot validate invoice ${c.label}.`}
                              detail={`Invoice ${c.label}: ${c.invoice}`}
                            />
                          );
                        }
                        return null;
                      })}
                      {bankingFullyMatches && !bankingHasAnyIssue && (
                        <BankingAlert
                          tone="success"
                          title="Banking details match supplier master."
                        />
                      )}
                    </div>
                    {(extractedBankAcct ||
                      extractedBankName ||
                      extractedBankSort ||
                      extractedBankAcctName ||
                      extractedBankSwift) && (
                      <div className="rounded-md border bg-muted/30 p-4">
                        {!data?.supplier_id ? (
                          <BankingAlert
                            tone="warning"
                            title="Link or create supplier before updating supplier master"
                          />
                        ) : !supplierHasAnyBank ? (
                          <div className="flex flex-wrap items-center justify-between gap-3">
                            <div>
                              <div className="text-sm font-medium">
                                Supplier master banking is empty
                              </div>
                              <div className="text-xs text-muted-foreground">
                                Use the extracted invoice banking details to populate the supplier master record.
                              </div>
                            </div>
                            <Button
                              size="sm"
                              onClick={() => setConfirmPopulate("fill")}
                              disabled={populatingSupplier}
                            >
                              Populate supplier master from invoice
                            </Button>
                          </div>
                        ) : !supplierBankMatchesExtracted ? (
                          <div className="flex flex-wrap items-center justify-between gap-3">
                            <div>
                              <div className="text-sm font-medium text-destructive">
                                Supplier master has different bank details
                              </div>
                              <div className="text-xs text-muted-foreground">
                                Review carefully — this will replace existing supplier master banking details.
                              </div>
                            </div>
                            <Button
                              size="sm"
                              variant="destructive"
                              onClick={() => setConfirmPopulate("overwrite")}
                              disabled={populatingSupplier}
                            >
                              Overwrite supplier master banking details
                            </Button>
                          </div>
                        ) : null}
                      </div>
                    )}
                    <div className="grid gap-5 lg:grid-cols-3">
                      <div>
                        <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                          Original (extracted)
                        </div>
                        <dl className="space-y-3">
                          <Field
                            label="Bank name"
                            value={extractedBankName || "—"}
                            readonly
                          />
                          <Field
                            label="Account number"
                            value={extractedBankAcct || "—"}
                            mono
                            readonly
                          />
                          <Field
                            label="Sort / routing"
                            value={extractedBankSort || "—"}
                            mono
                            readonly
                          />
                        </dl>
                      </div>
                      <div>
                        <div className="mb-2 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-primary">
                          Override (manual)
                          {hasOverrides && (
                            <span className="rounded-full bg-primary/15 px-1.5 py-0.5 text-[9px] font-semibold normal-case tracking-normal text-primary">
                              active
                            </span>
                          )}
                        </div>
                        <dl className="space-y-3">
                          <Field
                            label="Bank name"
                            value={overrideBankName || "—"}
                            override={!!overrideBankName}
                            overrideLabel={!!overrideBankName}
                            highlight={
                              bankingChecks[1].status === "mismatch" ||
                              bankingChecks[1].status === "missing-invoice"
                            }
                          />
                          <Field
                            label="Account number"
                            value={overrideBankAcct || "—"}
                            mono
                            override={!!overrideBankAcct}
                            overrideLabel={!!overrideBankAcct}
                            highlight={
                              acctCheck.status === "mismatch" ||
                              acctCheck.status === "missing-invoice"
                            }
                          />
                          <Field
                            label="Sort / routing"
                            value={overrideBankSort || "—"}
                            mono
                            override={!!overrideBankSort}
                            overrideLabel={!!overrideBankSort}
                            highlight={
                              bankingChecks[2].status === "mismatch" ||
                              bankingChecks[2].status === "missing-invoice"
                            }
                          />
                        </dl>
                      </div>
                      <div>
                        <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                          Supplier master
                        </div>
                        <dl className="space-y-3">
                          <Field
                            label="Bank name"
                            value={supplierBankName || "—"}
                          />
                          <Field
                            label="Account number"
                            value={supplierBankAcct || "—"}
                            mono
                            highlight={acctCheck.status === "mismatch"}
                          />
                          <Field
                            label="Sort / routing"
                            value={supplierBankSort || "—"}
                            mono
                          />
                        </dl>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              </TabsContent>

              <TabsContent value="supplier" className="mt-4">
                <Card className="card-elevated">
                  <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
                    <div>
                      <CardTitle className="text-base">Supplier</CardTitle>
                      <div className="mt-1 text-xs text-muted-foreground">
                        Extracted supplier identity and linked master record.
                      </div>
                    </div>
                    {!data?.supplier_id && (
                      <div className="flex shrink-0 flex-wrap justify-end gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => setLinkSupplierOpen(true)}
                          disabled={supplierMutating}
                        >
                          Link existing
                        </Button>
                        <Button
                          size="sm"
                          onClick={() => void handleCreateSupplier()}
                          disabled={supplierMutating}
                        >
                          {supplierMutating ? "Creating..." : "Add supplier"}
                        </Button>
                      </div>
                    )}
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <div>
                      <div className="mb-3 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                        Supplier identity
                      </div>
                      <div className="grid gap-3 sm:grid-cols-2">
                        {supplierIdentityFields.map((field) => (
                          <div key={field.key} className="grid gap-1.5">
                            <Label
                              htmlFor={`supplier-identity-${field.key}`}
                              className="flex items-center gap-2 text-sm font-medium"
                            >
                              {field.label}
                              {field.changed && (
                                <span className="rounded-full bg-primary/15 px-1.5 py-0.5 text-[9px] font-semibold text-primary">
                                  changed
                                </span>
                              )}
                            </Label>
                            {field.editable ? (
                              <Input
                                id={`supplier-identity-${field.key}`}
                                value={field.value}
                                onChange={(event) => setSupplierDraftField(field.key, event.target.value)}
                                className={field.changed ? "border-primary" : undefined}
                              />
                            ) : (
                              <Input
                                id={`supplier-identity-${field.key}`}
                                value={field.value || "—"}
                                readOnly
                                className={field.changed ? "border-primary bg-muted/40" : "bg-muted/40"}
                              />
                            )}
                          </div>
                        ))}
                      </div>
                    </div>

                    {!data?.supplier_id && (
                      supplierAdditionalDraftFields.length > 0 ? (
                        <>
                          <Separator />
                          <div>
                            <div className="mb-3 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                              Supplier contact and profile
                            </div>
                        <div className="grid gap-3 sm:grid-cols-2">
                          {supplierAdditionalDraftFields.map((field) => (
                            <div key={field.key} className="grid gap-1.5">
                              <Label
                                htmlFor={`supplier-draft-${field.key}`}
                                className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground"
                              >
                                {field.label}
                              </Label>
                              <Input
                                id={`supplier-draft-${field.key}`}
                                value={supplierDraft[field.key] ?? ""}
                                onChange={(event) => setSupplierDraftField(field.key, event.target.value)}
                              />
                            </div>
                          ))}
                        </div>
                          </div>
                        </>
                      ) : (
                        <p className="text-sm text-muted-foreground">
                          No additional supplier details parsed.
                        </p>
                      )
                    )}

                    <Separator />
                    {!data?.supplier_id || !supplier ? (
                      <p className="text-sm text-muted-foreground">
                        No supplier master linked yet.
                      </p>
                    ) : (
                      <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
                        <Field label="Supplier name" value={supplierName} />
                        <Field
                          label="VAT number"
                          value={
                            pickStr(supplier, [
                              "vat_number",
                              "vat",
                              "tax_number",
                              "tax_id",
                            ]) || "—"
                          }
                        />
                        <Field
                          label="Company registration"
                          value={
                            pickStr(supplier, [
                              "company_registration_number",
                              "company_number",
                              "registration_number",
                              "company_reg",
                            ]) || "—"
                          }
                        />
                        <Field
                          label="Payment terms"
                          value={
                            pickStr(supplier, [
                              "payment_terms",
                              "terms",
                              "payment_terms_days",
                            ]) || "—"
                          }
                        />
                        <Field
                          label="Email"
                          value={
                            pickStr(supplier, [
                              "email",
                              "contact_email",
                              "billing_email",
                            ]) || "—"
                          }
                        />
                        <Field
                          label="Phone"
                          value={
                            pickStr(supplier, ["phone", "phone_number", "tel"]) ||
                            "—"
                          }
                        />
                        <Field
                          label="Bank name"
                          value={supplierBankName || "—"}
                        />
                        <Field
                          label="Bank account"
                          value={supplierBankAcct || "—"}
                          mono
                        />
                        <Field
                          label="Sort / routing"
                          value={supplierBankSort || "—"}
                          mono
                        />
                      </dl>
                    )}
                  </CardContent>
                </Card>
              </TabsContent>

              <TabsContent value="audit" className="mt-4">
                <Card className="card-elevated">
                  <CardHeader>
                    <CardTitle className="text-base">Audit trail</CardTitle>
                  </CardHeader>
                  <CardContent className="p-0">
                    <InvoiceAuditTrail rows={auditRows ?? []} />
                  </CardContent>
                </Card>
              </TabsContent>
              </Tabs>
            </div>
          </div>

          {/* Line Items full width */}
          <LineItemsCard
            invoiceId={(data?.id as string) ?? invoiceId}
            parsed={parsed}
            subtotal={subtotal ?? null}
            currency={currency}
            reextractDiagnostic={lineItemsReextractDiagnostic}
          />
        </div>
      )}

      <EditBankingDialog
        open={editOpen}
        onOpenChange={setEditOpen}
        saving={saving}
        extracted={{
          bank_account_number: extractedBankAcct,
          bank_name: extractedBankName,
          sort_code: extractedBankSort,
        }}
        current={{
          bank_account_number: overrideBankAcct,
          bank_name: overrideBankName,
          sort_code: overrideBankSort,
        }}
        onSave={handleSaveOverrides}
      />

      <AlertDialog
        open={confirmDelete !== null}
        onOpenChange={(o) => !o && setConfirmDelete(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirmDelete === "all"
                ? "Delete upload and extracted invoice?"
                : "Delete this extracted invoice?"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirmDelete === "all"
                ? "This permanently removes the extracted invoice, the original upload record, and the stored file. This cannot be undone."
                : "This removes the extracted invoice row. The original upload and file are preserved."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={deleting}
              onClick={(e) => {
                e.preventDefault();
                if (confirmDelete) handleDelete(confirmDelete);
              }}
            >
              {deleting ? "Deleting…" : "Delete"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={confirmPopulate !== null}
        onOpenChange={(o) => !o && !populatingSupplier && setConfirmPopulate(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirmPopulate === "overwrite"
                ? "Overwrite supplier master banking details?"
                : "Update supplier master banking?"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              This will update the supplier master banking details using the extracted invoice banking details. Continue?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={populatingSupplier}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={populatingSupplier}
              onClick={(e) => {
                e.preventDefault();
                if (confirmPopulate) handlePopulateSupplierBanking(confirmPopulate);
              }}
            >
              {populatingSupplier ? "Updating…" : "Continue"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <LinkSupplierDialog
        open={linkSupplierOpen}
        onOpenChange={(o) => !supplierMutating && setLinkSupplierOpen(o)}
        organisationId={currentOrgId ?? null}
        suggestedName={pickStr(data, ["supplier_name_extracted"]) ?? ""}
        saving={supplierMutating}
        onSelect={handleLinkSupplier}
      />
    </>
  );
}

function EditableExtractedCard({
  invoiceId,
  data,
  currency,
  onSaved,
}: {
  invoiceId: string;
  data: AnyRec;
  currency: string;
  onSaved: () => void;
}) {
  const { currentOrgId } = useOrg();
  const { user } = useAuth();
  const initial = useMemo(
    () => ({
      invoice_number:
        pickStr(data, ["invoice_number", "reference", "number"]) ?? "",
      supplier_name: pickStr(data, ["supplier_name_extracted", "supplier_name", "supplier", "vendor"]) ?? "",
      invoice_date: pickStr(data, ["invoice_date", "issue_date", "date"]) ?? "",
      due_date: pickStr(data, ["due_date", "payment_due_date"]) ?? "",
      subtotal:
        pickNum(data, ["subtotal", "net_amount", "sub_total"])?.toString() ?? "",
      vat_amount:
        pickNum(data, ["vat_amount", "tax_amount", "tax", "vat"])?.toString() ??
        "",
      total_amount:
        pickNum(data, ["total_amount", "total", "amount", "grand_total"])?.toString() ??
        "",
      currency,
    }),
    [data, currency],
  );
  const [form, setForm] = useState(initial);
  const [originalPayload, setOriginalPayload] = useState(() => buildPayloadFrom(initial));
  const [saving, setSaving] = useState(false);
  const [approving, setApproving] = useState(false);

  // Reset form when the extracted row is refreshed, including same-id re-extractions.
  useEffect(() => {
    setForm(initial);
    setOriginalPayload(buildPayloadFrom(initial));
  }, [initial]);

  function set<K extends keyof typeof form>(k: K, v: string) {
    setForm((prev) => ({ ...prev, [k]: v }));
  }

  function buildPayloadFrom(f: typeof initial) {
    const num = (s: string) => (s.trim() === "" ? null : Number(s));
    return {
      invoice_number: f.invoice_number || null,
      supplier_name_extracted: f.supplier_name || null,
      invoice_date: f.invoice_date || null,
      due_date: f.due_date || null,
      subtotal: num(f.subtotal),
      tax_amount: num(f.vat_amount),
      total_amount: num(f.total_amount),
      currency: f.currency || null,
    };
  }

  function buildPayload() {
    return buildPayloadFrom(form);
  }

  async function recordFeedback(
    before: Record<string, unknown>,
    after: Record<string, unknown>,
  ) {
    const fields = [
      "supplier_name_extracted",
      "invoice_number",
      "invoice_date",
      "due_date",
      "subtotal",
      "tax_amount",
      "total_amount",
      "currency",
      "bank_account_name_extracted",
      "bank_name_extracted",
      "bank_account_number_extracted",
      "bank_branch_code_extracted",
      "bank_swift_code_extracted",
    ];
    const norm = (v: unknown) =>
      v === null || v === undefined || v === "" ? null : v;
    const rows: Record<string, unknown>[] = [];
    for (const field of fields) {
      const beforeVal = norm(before[field]);
      const afterVal = norm(after[field]);
      if (String(beforeVal ?? "") === String(afterVal ?? "")) continue;
      rows.push({
        organisation_id: currentOrgId,
        invoice_raw_id: (data?.invoice_raw_id as string | undefined) ?? null,
        invoice_extracted_id: invoiceId,
        supplier_id: (data?.supplier_id as string | undefined) ?? null,
        field_name: field,
        extracted_value: beforeVal === null ? null : String(beforeVal),
        corrected_value: afterVal === null ? null : String(afterVal),
        source_text: null,
        layout_type: (data?.layout_type as string | undefined) ?? null,
        correction_type: "manual",
        created_by: user?.id ?? null,
      });
    }
    if (rows.length === 0) return true;
    const { error } = await supabase
      .from("invoice_extraction_feedback")
      .insert(rows);
    if (error) {
      // eslint-disable-next-line no-console
      console.error("[invoice feedback] insert failed", error);
      return false;
    }
    return true;
  }

  async function handleSave() {
    setSaving(true);
    try {
      const payload = buildPayload();
      const before = originalPayload;
      const { error } = await supabase
        .from("invoices_extracted")
        .update(payload)
        .eq("id", invoiceId);
      if (error) throw error;
      toast.success("Changes saved");
      const ok = await recordFeedback(before, payload);
      if (!ok) {
        toast.warning("Invoice saved, but correction feedback was not recorded.");
      }
      setOriginalPayload(payload);
      onSaved();
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error("[invoice review] save failed", e);
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function handleApprove() {
    setApproving(true);
    try {
      const base = buildPayload();
      const before = originalPayload;
      // 1. Save changed fields first
      const { error: saveErr } = await supabase
        .from("invoices_extracted")
        .update(base)
        .eq("id", invoiceId);
      if (saveErr) throw saveErr;

      // 2. Insert correction feedback for changed fields
      const ok = await recordFeedback(before, base);
      if (!ok) {
        toast.warning("Invoice saved, but correction feedback was not recorded.");
      }

      // 3. Apply approval status
      const { error: approveErr } = await supabase
        .from("invoices_extracted")
        .update({
          approval_status: "approved",
          review_status: "approved",
          approved_at: new Date().toISOString(),
          approved_by: user?.id ?? null,
        })
        .eq("id", invoiceId);
      if (approveErr) throw approveErr;

      toast.success("Invoice approved");
      setOriginalPayload(base);
      // 4. Refetch
      onSaved();
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error("[invoice review] approve failed", e);
      toast.error((e as Error).message);
    } finally {
      setApproving(false);
    }
  }

  return (
    <Card className="card-elevated">
      <CardHeader>
        <CardTitle className="text-base">Extracted data</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="grid gap-1.5">
            <Label htmlFor="f-invoice-number">Invoice number</Label>
            <Input
              id="f-invoice-number"
              value={form.invoice_number}
              onChange={(e) => set("invoice_number", e.target.value)}
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="f-supplier">Supplier</Label>
            <Input
              id="f-supplier"
              value={form.supplier_name}
              onChange={(e) => set("supplier_name", e.target.value)}
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="f-invoice-date">Invoice date</Label>
            <Input
              id="f-invoice-date"
              type="date"
              value={form.invoice_date ? form.invoice_date.slice(0, 10) : ""}
              onChange={(e) => set("invoice_date", e.target.value)}
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="f-due-date">Due date</Label>
            <Input
              id="f-due-date"
              type="date"
              value={form.due_date ? form.due_date.slice(0, 10) : ""}
              onChange={(e) => set("due_date", e.target.value)}
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="f-subtotal">Subtotal</Label>
            <Input
              id="f-subtotal"
              inputMode="decimal"
              value={form.subtotal}
              onChange={(e) => set("subtotal", e.target.value)}
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="f-vat">VAT / tax amount</Label>
            <Input
              id="f-vat"
              inputMode="decimal"
              value={form.vat_amount}
              onChange={(e) => set("vat_amount", e.target.value)}
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="f-total">Total amount</Label>
            <Input
              id="f-total"
              inputMode="decimal"
              value={form.total_amount}
              onChange={(e) => set("total_amount", e.target.value)}
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="f-currency">Currency</Label>
            <Input
              id="f-currency"
              value={form.currency}
              onChange={(e) => set("currency", e.target.value)}
            />
          </div>
        </div>
        <Separator className="my-5" />
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Link to="/invoices/upload">
            <Button variant="ghost" size="sm" className="gap-2">
              <ArrowLeft className="h-4 w-4" /> Back to upload queue
            </Button>
          </Link>
          <Button
            variant="outline"
            size="sm"
            className="gap-2"
            onClick={handleSave}
            disabled={saving || approving}
          >
            <Save className="h-4 w-4" />
            {saving ? "Saving…" : "Save changes"}
          </Button>
          <Button
            size="sm"
            className="gap-2"
            onClick={handleApprove}
            disabled={saving || approving}
          >
            <CheckCircle2 className="h-4 w-4" />
            {approving ? "Approving…" : "Approve invoice"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function Field({
  label,
  value,
  mono,
  emphasis,
  highlight,
  readonly,
  override,
  overrideLabel,
}: {
  label: string;
  value: string;
  mono?: boolean;
  emphasis?: boolean;
  highlight?: boolean;
  readonly?: boolean;
  override?: boolean;
  overrideLabel?: boolean;
}) {
  const tone = highlight
    ? "border-destructive/40 bg-destructive/5"
    : override
      ? "border-primary/40 bg-primary/5"
      : readonly
        ? "border-dashed bg-surface-muted/20"
        : "bg-surface-muted/30";
  return (
    <div className={`rounded-lg border px-3 py-2.5 ${tone}`}>
      <dt className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
        <span>{label}</span>
        {overrideLabel && (
          <span className="rounded-full bg-primary/15 px-1.5 py-0.5 text-[9px] font-semibold tracking-normal text-primary">
            Override active
          </span>
        )}
      </dt>
      <dd
        className={`mt-0.5 break-words text-sm ${mono ? "font-mono tabular-nums" : ""} ${emphasis ? "text-base font-semibold" : "font-medium"} ${highlight ? "text-destructive" : ""} ${override && !highlight ? "text-primary" : ""}`}
      >
        {value}
      </dd>
    </div>
  );
}

function BankingAlert({
  tone,
  title,
  detail,
}: {
  tone: "destructive" | "warning" | "success";
  title: string;
  detail?: string;
}) {
  const styles = {
    destructive: "border-destructive/30 bg-destructive/10 text-destructive",
    warning: "border-warning/40 bg-warning/10 text-warning-foreground",
    success: "border-success/40 bg-success/10 text-success",
  }[tone];
  const Icon = tone === "success" ? CheckCircle2 : AlertTriangle;
  return (
    <div className={`flex items-start gap-2 rounded-md border p-3 text-sm ${styles}`}>
      <Icon className="mt-0.5 h-4 w-4 shrink-0" />
      <div className="min-w-0">
        <div className="font-medium">{title}</div>
        {detail && <div className="mt-0.5 text-xs opacity-90 break-words">{detail}</div>}
      </div>
    </div>
  );
}

type AuditEntry = {
  field: string;
  oldValue: string;
  newValue: string;
  actor: string;
  createdAt: string;
};

function AuditTrail({
  rows,
  fallback,
}: {
  rows: AnyRec[];
  fallback: { label: string; detail: string }[];
}) {
  const entries: AuditEntry[] = [];

  for (const r of rows) {
    const eventType = pickStr(r, ["event_type", "type"]) || "event";
    const createdAt = pickStr(r, ["created_at", "inserted_at"]) || "";
    const actor =
      pickStr(r, ["actor_email", "user_email"]) ||
      pickStr(r, ["actor_id", "user_id"]) ||
      "system";
    const before = (r.before ?? {}) as AnyRec;
    const after = (r.after ?? {}) as AnyRec;

    if (eventType === "banking_override_saved") {
      const fieldMap: Array<[string, string, string]> = [
        ["Bank account number", "bank_account_number", "override_bank_account_number"],
        ["Bank name", "bank_name", "override_bank_name"],
        ["Sort / routing code", "sort_code", "override_sort_code"],
      ];
      for (const [label, beforeKey, afterKey] of fieldMap) {
        const oldV = pickStr(before, [beforeKey]) ?? "";
        const newV = pickStr(after, [afterKey]) ?? "";
        if (oldV === newV) continue;
        entries.push({
          field: label,
          oldValue: oldV || "—",
          newValue: newV || "—",
          actor,
          createdAt,
        });
      }
    } else {
      const field = pickStr(r, ["field_name", "field"]) || humanEventLabel(eventType);
      const oldV = pickStr(r, ["old_value"]) ?? JSON.stringify(before);
      const newV = pickStr(r, ["new_value"]) ?? JSON.stringify(after);
      entries.push({
        field,
        oldValue: oldV && oldV !== "{}" ? oldV : "—",
        newValue: newV && newV !== "{}" ? newV : "—",
        actor,
        createdAt,
      });
    }
  }

  if (entries.length === 0) {
    return (
      <ul className="space-y-3 p-6 text-sm">
        {fallback.map((e, i) => (
          <li
            key={i}
            className="flex items-start gap-3 rounded-md border bg-surface-muted/30 px-3 py-2.5"
          >
            <div className="mt-1 h-2 w-2 shrink-0 rounded-full bg-primary" />
            <div>
              <div className="font-medium">{e.label}</div>
              <div className="text-xs text-muted-foreground">{e.detail}</div>
            </div>
          </li>
        ))}
      </ul>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="bg-surface-muted/60">
          <tr className="border-b text-left text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            <th className="px-4 py-2.5">Field</th>
            <th className="px-4 py-2.5">Old value</th>
            <th className="px-4 py-2.5">New value</th>
            <th className="px-4 py-2.5">User</th>
            <th className="px-4 py-2.5">When</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e, i) => (
            <tr key={i} className="border-b last:border-0 hover:bg-accent/30">
              <td className="px-4 py-2.5 font-medium">{e.field}</td>
              <td className="px-4 py-2.5 font-mono text-xs text-muted-foreground line-through">
                {e.oldValue}
              </td>
              <td className="px-4 py-2.5 font-mono text-xs text-primary">
                {e.newValue}
              </td>
              <td className="px-4 py-2.5 text-xs">{e.actor}</td>
              <td className="px-4 py-2.5 text-xs text-muted-foreground">
                {e.createdAt ? fmtDateTime(e.createdAt) : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function InvoiceAuditTrail({ rows }: { rows: AnyRec[] }) {
  if (rows.length === 0) {
    return (
      <ul className="space-y-3 p-6 text-sm">
        <li className="flex items-start gap-3 rounded-md border bg-surface-muted/30 px-3 py-2.5">
          <div className="mt-1 h-2 w-2 shrink-0 rounded-full bg-muted-foreground" />
          <div>
            <div className="font-medium">No audit events</div>
            <div className="text-xs text-muted-foreground">
              Re-extraction events will appear here once recorded.
            </div>
          </div>
        </li>
      </ul>
    );
  }

  return (
    <div className="space-y-3 p-4">
      {rows.map((row, index) => {
        const eventType = pickStr(row, ["event_type", "type"]) || "event";
        const stage = pickStr(row, ["stage"]);
        const createdAt = pickStr(row, ["created_at", "inserted_at"]);
        const notes = pickStr(row, ["notes", "note", "message"]);
        const confidenceBefore = pickNum(row, ["confidence_before"]);
        const confidenceAfter = pickNum(row, ["confidence_after"]);
        const newValue = row.new_value;

        return (
          <div
            key={`${eventType}-${createdAt ?? index}`}
            className="rounded-lg border bg-surface-muted/20 p-3"
          >
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <div className="font-medium">{humanEventLabel(eventType)}</div>
                <div className="mt-0.5 font-mono text-xs text-muted-foreground">
                  {eventType}
                </div>
              </div>
              <div className="text-right text-xs text-muted-foreground">
                {createdAt ? fmtDateTime(createdAt) : "—"}
              </div>
            </div>

            <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
              <AuditValue label="Stage" value={stage ?? "—"} />
              <AuditValue
                label="Confidence"
                value={formatConfidenceChange(confidenceBefore, confidenceAfter)}
              />
            </div>

            {notes && (
              <div className="mt-3 rounded-md border border-dashed bg-background/60 px-3 py-2 text-sm">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Notes
                </div>
                <div className="mt-1 whitespace-pre-wrap break-words">{notes}</div>
              </div>
            )}

            {newValue != null && (
              <div className="mt-3">
                <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                  New value
                </div>
                <pre className="max-h-72 overflow-auto rounded-md border bg-background p-3 text-xs leading-relaxed">
                  {formatJsonValue(newValue)}
                </pre>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function AuditValue({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border bg-background/60 px-3 py-2">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="mt-0.5 break-words text-sm font-medium">{value}</div>
    </div>
  );
}

function DocumentPreview({
  row,
  raw,
  pages,
}: {
  row: AnyRec;
  raw: AnyRec | null;
  pages: AnyRec[];
}) {
  const BUCKET = "invoices";
  const rawPath =
    pickStr(raw, ["file_path", "storage_path", "path"]) ||
    pickStr(row, ["file_path", "storage_path", "path"]);
  const filePath = normalizeStoragePath(rawPath, BUCKET);
  const directUrl =
    pickStr(row, ["document_url", "source_url", "file_url", "pdf_url", "raw_url"]) ||
    pickStr(raw, ["document_url", "source_url", "file_url", "pdf_url", "raw_url"]);
  const directOriginalUrl = rawPath && isHttpUrl(rawPath) ? rawPath : directUrl;
  const rawPreviewPath = pickStr(raw, ["preview_path", "original_preview_path"]);
  const rawProcessedPreviewPath = pickStr(raw, ["processed_preview_path"]);
  const hasProcessedPreview =
    Boolean(rawProcessedPreviewPath) ||
    pages.some((p) => Boolean(pickStr(p, ["processed_preview_path"])));

  const rawId =
    pickStr(raw, ["id"]) ||
    pickStr(row, ["invoice_raw_id"]) ||
    null;
  const proxyUrl = rawId && FASTAPI_URL
    ? `${FASTAPI_URL.replace(/\/+$/, "")}/api/invoices/raw/${rawId}/file`
    : null;

  const [page, setPage] = useState(0);
  type ZoomMode = "fit-width" | "fit-page" | "custom";
  const [zoomMode, setZoomMode] = useState<ZoomMode>("fit-width");
  const [zoom, setZoom] = useState(1);
  const [originalError, setOriginalError] = useState(false);
  const [previewError, setPreviewError] = useState(false);
  const [previewLoaded, setPreviewLoaded] = useState(false);
  const [resolvedFileUrl, setResolvedFileUrl] = useState<string | null>(null);
  const [resolvedOriginalPreviewUrl, setResolvedOriginalPreviewUrl] = useState<string | null>(null);
  const [resolvedProcessedPreviewUrl, setResolvedProcessedPreviewUrl] = useState<string | null>(null);
  const [resolvingAssets, setResolvingAssets] = useState(false);

  const pageCount = Math.max(1, pages.length);
  const currentPage = pages[page] ?? null;
  const currentOriginalPreviewPath =
    pickStr(currentPage, ["original_preview_path", "preview_path"]) ||
    (page === 0 ? rawPreviewPath : null);
  const currentProcessedPreviewPath =
    pickStr(currentPage, ["processed_preview_path"]) ||
    (page === 0 ? rawProcessedPreviewPath : null);

  useEffect(() => {
    setPage((p) => Math.min(p, pageCount - 1));
  }, [pageCount]);

  useEffect(() => {
    let cancelled = false;
    setResolvingAssets(true);
    setResolvedFileUrl(null);
    setResolvedOriginalPreviewUrl(null);
    setResolvedProcessedPreviewUrl(null);

    Promise.all([
      proxyUrl || directOriginalUrl
        ? Promise.resolve(null)
        : resolveStorageAssetUrl(filePath, BUCKET),
      resolveStorageAssetUrl(currentOriginalPreviewPath, BUCKET),
      resolveStorageAssetUrl(currentProcessedPreviewPath, BUCKET),
    ])
      .then(([fileUrl, originalPreviewUrl, processedPreviewUrl]) => {
        if (cancelled) return;
        setResolvedFileUrl(fileUrl);
        setResolvedOriginalPreviewUrl(originalPreviewUrl);
        setResolvedProcessedPreviewUrl(processedPreviewUrl);
        setResolvingAssets(false);
      })
      .catch((assetError) => {
        if (cancelled) return;
        console.warn("Preview asset resolution failed", assetError);
        setResolvingAssets(false);
      });

    return () => {
      cancelled = true;
    };
  }, [
    proxyUrl,
    directOriginalUrl,
    filePath,
    currentOriginalPreviewPath,
    currentProcessedPreviewPath,
  ]);

  useEffect(() => {
    setOriginalError(false);
    setPreviewError(false);
    setPreviewLoaded(false);
  }, [
    hasProcessedPreview,
    page,
    proxyUrl,
    directOriginalUrl,
    resolvedFileUrl,
    resolvedOriginalPreviewUrl,
    resolvedProcessedPreviewUrl,
  ]);

  const fitWidth = () => { setZoomMode("fit-width"); setZoom(1); };
  const fitPage = () => { setZoomMode("fit-page"); setZoom(1); };
  const zoomIn = () => { setZoomMode("custom"); setZoom((z) => Math.min(4, +(z + 0.25).toFixed(2))); };
  const zoomOut = () => { setZoomMode("custom"); setZoom((z) => Math.max(0.25, +(z - 0.25).toFixed(2))); };
  const zoomReset = fitWidth;

  const originalUrl = proxyUrl || directOriginalUrl || resolvedFileUrl;
  const originalPathForType = filePath ?? originalUrl;
  const originalCanRender =
    Boolean(originalUrl) &&
    (isImagePath(originalPathForType) || isPdfPath(originalPathForType));
  const originalImage = Boolean(originalUrl) && isImagePath(originalPathForType);
  const fallbackPreviewUrl =
    hasProcessedPreview ? resolvedProcessedPreviewUrl : resolvedOriginalPreviewUrl;
  const shouldShowOriginal =
    !hasProcessedPreview && originalCanRender && !originalError;
  const shouldShowPreviewImage =
    hasProcessedPreview ||
    !shouldShowOriginal;
  const displayedZoomPct =
    zoomMode === "custom" ? Math.round(zoom * 100) : zoomMode === "fit-page" ? 0 : 100;
  const imgStyle: React.CSSProperties =
    zoomMode === "fit-page"
      ? { maxHeight: "700px", maxWidth: "100%", width: "auto", height: "auto" }
      : zoomMode === "fit-width"
        ? { width: "100%", height: "auto", maxWidth: "100%" }
        : { width: `${zoom * 100}%`, height: "auto", maxWidth: "none" };

  const openInNewTab = () => {
    if (!originalUrl) return;
    window.open(originalUrl, "_blank", "noopener,noreferrer");
  };

  const downloadOriginal = () => {
    if (!originalUrl) return;
    const a = document.createElement("a");
    a.href = originalUrl;
    a.download =
      pickStr(raw, ["file_name", "filename", "name"]) ||
      pickStr(row, ["file_name", "filename", "name"]) ||
      "invoice-document";
    a.rel = "noopener noreferrer";
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {!hasProcessedPreview && (
          <Button type="button" variant="outline" size="sm" onClick={openInNewTab} disabled={!originalUrl}>
            Open original document
          </Button>
        )}
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="gap-2"
          onClick={downloadOriginal}
          disabled={!originalUrl}
        >
          <Download className="h-4 w-4" />
          Download original document
        </Button>

        <div className="flex items-center gap-1 rounded-md border bg-background p-1">
          <Button type="button" variant="ghost" size="icon" className="h-7 w-7" onClick={zoomOut} aria-label="Zoom out">
            <ZoomOut className="h-4 w-4" />
          </Button>
          <span className="min-w-[3rem] text-center text-xs tabular-nums">
            {zoomMode === "fit-page" ? "Fit" : `${displayedZoomPct}%`}
          </span>
          <Button type="button" variant="ghost" size="icon" className="h-7 w-7" onClick={zoomIn} aria-label="Zoom in">
            <ZoomIn className="h-4 w-4" />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            onClick={zoomReset}
            aria-label="Reset zoom"
          >
            <Maximize2 className="h-4 w-4" />
          </Button>
        </div>
        <Button
          type="button"
          variant={zoomMode === "fit-width" ? "secondary" : "outline"}
          size="sm"
          onClick={fitWidth}
        >
          Fit width
        </Button>
        <Button
          type="button"
          variant={zoomMode === "fit-page" ? "secondary" : "outline"}
          size="sm"
          onClick={fitPage}
        >
          Fit page
        </Button>

        {pageCount > 1 && (
          <div className="flex items-center gap-1 rounded-md border bg-background p-1">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page <= 0}
              aria-label="Previous page"
            >
              <ChevronLeft className="h-4 w-4" />
            </Button>
            <span className="min-w-[5.5rem] text-center text-xs tabular-nums">
              Page {page + 1} of {pageCount}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
              disabled={page >= pageCount - 1}
              aria-label="Next page"
            >
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        )}
      </div>

      <div
        className="rounded-md border bg-surface-muted/20 overflow-auto"
        style={{ maxHeight: "720px", maxWidth: "100%" }}
      >
        {shouldShowOriginal && originalUrl && originalImage ? (
          <div className="p-2 inline-block min-w-full">
            <img
              src={originalUrl}
              alt="Invoice document preview"
              onLoad={() => setPreviewLoaded(true)}
              onError={() => setOriginalError(true)}
              style={imgStyle}
              className="block"
            />
            {!previewLoaded && <div className="sr-only">Loading preview...</div>}
          </div>
        ) : shouldShowOriginal && originalUrl ? (
          <iframe
            title="Invoice document preview"
            src={originalUrl}
            onError={() => setOriginalError(true)}
            className="h-[700px] w-full bg-background"
          />
        ) : shouldShowPreviewImage && fallbackPreviewUrl && !previewError ? (
          <div className="p-2 inline-block min-w-full">
            <img
              src={fallbackPreviewUrl}
              alt="Invoice document preview"
              onLoad={() => setPreviewLoaded(true)}
              onError={() => setPreviewError(true)}
              style={imgStyle}
              className="block"
            />
            {!previewLoaded && <div className="sr-only">Loading preview...</div>}
          </div>
        ) : resolvingAssets ? (
          <div className="flex flex-col items-center justify-center gap-3 py-12 text-sm text-muted-foreground">
            <div>Loading document preview...</div>
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center gap-3 py-12 text-sm text-muted-foreground">
            <FileWarning className="h-6 w-6" />
            <div className="text-center">
              <div className="font-medium text-foreground">
                No document preview is available, but the source file record exists.
              </div>
              {rawId ? null : (
                <div className="text-xs">No invoice_raw_id available for this invoice.</div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function isHttpUrl(value: string | null | undefined): boolean {
  return Boolean(value && /^https?:\/\//i.test(value.trim()));
}

function normalizeStoragePath(value: string | null | undefined, bucket: string): string | null {
  if (!value) return null;
  let p = value.trim();
  if (!p) return null;

  for (const marker of [
    `/object/public/${bucket}/`,
    `/object/sign/${bucket}/`,
    `/object/authenticated/${bucket}/`,
  ]) {
    const idx = p.indexOf(marker);
    if (idx >= 0) {
      p = p.slice(idx + marker.length).split("?")[0];
      break;
    }
  }

  if (isHttpUrl(p)) return p;
  if (p.startsWith(`${bucket}/`)) p = p.slice(bucket.length + 1);
  return p.replace(/^\/+/, "");
}

async function resolveStorageAssetUrl(
  value: string | null | undefined,
  bucket: string,
): Promise<string | null> {
  const path = normalizeStoragePath(value, bucket);
  if (!path) return null;
  if (isHttpUrl(path)) return path;

  const { data, error } = await supabase.storage.from(bucket).createSignedUrl(path, 60 * 60);
  if (!error && data?.signedUrl) return data.signedUrl;

  const { data: publicData } = supabase.storage.from(bucket).getPublicUrl(path);
  return publicData.publicUrl || null;
}

function isImagePath(value: string | null | undefined): boolean {
  return /\.(png|jpe?g|webp|gif)(\?|#|$)/i.test(value ?? "");
}

function isPdfPath(value: string | null | undefined): boolean {
  return /\.pdf(\?|#|$)/i.test(value ?? "");
}

// -----------------------------------------------------------------------------
// Line items
// -----------------------------------------------------------------------------
// NOTE: There is no dedicated invoice_lines / invoice_line_items table in the
// generated Supabase types yet. Until one exists, line items are rendered from
// the backend extraction response (persisted as JSON onto invoices_raw.parsed
// when that column is available). Edits made here are local-only — the
// "Save line item changes" button only logs the payload and does NOT persist.
// TODO: When a line-items table is introduced, wire `handleSaveAll` to
// upsert each row by (invoice_id, line_no).
type LineItem = {
  id?: string | null;
  supplier_stock_number?: string | null;
  description?: string | null;
  quantity?: number | string | null;
  unit_count?: number | string | null;
  unit_of_measure?: string | null;
  unit_price?: number | string | null;
  discount?: number | string | null;
  tax_amount?: number | string | null;
  line_total?: number | string | null;
  amount?: number | string | null;
  confidence_score?: number | null;
  review_status?: string | null;
};

const SUMMARY_KEYWORDS = [
  "subtotal",
  "sub total",
  "vat",
  "tax",
  "total",
  "amount due",
  "amount paid",
  "less amount",
  "balance due",
  "grand total",
];

function isSummaryRow(item: LineItem): boolean {
  const desc = String(item.description ?? "").trim().toLowerCase();
  if (!desc) return false;
  return SUMMARY_KEYWORDS.some((k) => desc === k || desc.startsWith(k));
}

function toNum(v: unknown): number | null {
  if (typeof v === "number" && !Number.isNaN(v)) return v;
  if (typeof v === "string" && v.trim() && !Number.isNaN(Number(v))) return Number(v);
  return null;
}

function LineItemsReextractDiagnosticBanner({
  diagnostic,
  currency,
}: {
  diagnostic: LineItemsReextractDiagnostic | null | undefined;
  currency: string;
}) {
  const message = lineItemsDiagnosticMessage(diagnostic, currency);
  if (!message) return null;
  const Icon = message.tone === "success" ? CheckCircle2 : AlertTriangle;
  const toneClass =
    message.tone === "success"
      ? "border-success/30 bg-success/10 text-success"
      : message.tone === "warning"
        ? "border-warning/40 bg-warning/10 text-warning-foreground"
        : "border-border bg-surface-muted/40 text-muted-foreground";

  return (
    <div className={`flex items-start gap-2 rounded-md border px-3 py-2 text-sm ${toneClass}`}>
      <Icon className="mt-0.5 h-4 w-4 shrink-0" />
      <span>{message.text}</span>
    </div>
  );
}

function LineItemsCard({
  invoiceId,
  parsed,
  subtotal,
  currency,
  reextractDiagnostic,
}: {
  invoiceId: string;
  parsed: AnyRec;
  subtotal: number | null;
  currency: string;
  reextractDiagnostic?: LineItemsReextractDiagnostic | null;
}) {
  const { data: persistedItems = [] } = useQuery({
    queryKey: ["invoice_line_items", invoiceId],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("invoice_line_items")
        .select("*")
        .eq("invoice_extracted_id", invoiceId)
        .order("created_at", { ascending: true })
        .order("id", { ascending: true });
      if (error) {
        // eslint-disable-next-line no-console
        console.warn("invoice_line_items fetch failed", error);
        return [] as LineItem[];
      }
      return (data ?? []) as LineItem[];
    },
    enabled: !!invoiceId,
  });

  const parsedItems: LineItem[] = (() => {
    const candidates = [
      parsed?.line_items,
      parsed?.lineItems,
      parsed?.items,
      parsed?.lines,
    ];
    for (const c of candidates) {
      if (Array.isArray(c)) return c as LineItem[];
    }
    return [];
  })();

  const hasPersisted = persistedItems.length > 0;
  const rawItems = hasPersisted ? persistedItems : parsedItems;
  const filteredInitial = rawItems.filter((it) => !isSummaryRow(it));

  const [items, setItems] = useState<LineItem[]>(filteredInitial);

  // Reset when underlying data changes
  useEffect(() => {
    setItems(filteredInitial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(rawItems)]);

  const reconciliation = (() => {
    const sum = items.reduce((acc, it) => {
      const t = toNum(it.line_total) ?? toNum(it.amount);
      return acc + (t ?? 0);
    }, 0);
    const diff =
      subtotal != null ? Math.round((sum - subtotal) * 100) / 100 : null;
    const matches = subtotal != null ? Math.abs(sum - subtotal) < 0.01 : null;
    return { sum: Math.round(sum * 100) / 100, subtotal, diff, matches };
  })();

  // eslint-disable-next-line no-console
  console.log("LINE ITEM RECONCILIATION", reconciliation);

  function update<K extends keyof LineItem>(idx: number, k: K, v: LineItem[K]) {
    setItems((prev) => prev.map((it, i) => (i === idx ? { ...it, [k]: v } : it)));
  }
  function removeRow(idx: number) {
    setItems((prev) => prev.filter((_, i) => i !== idx));
  }
  function addRow() {
    setItems((prev) => [
      ...prev,
      {
        description: "",
        quantity: null,
        unit_price: null,
        line_total: null,
        review_status: "pending",
      },
    ]);
  }
  function handleSaveAll() {
    const payload = items.map((it) => ({
      supplier_stock_number: it.supplier_stock_number ?? null,
      description: it.description ?? null,
      quantity: toNum(it.quantity),
      unit_count: toNum(it.unit_count),
      unit_of_measure: it.unit_of_measure ?? null,
      unit_price: toNum(it.unit_price),
      discount: toNum(it.discount),
      line_total: toNum(it.line_total) ?? toNum(it.amount),
      confidence_score: it.confidence_score ?? null,
      review_status: it.review_status ?? "pending",
    }));
    // eslint-disable-next-line no-console
    console.log("LINE ITEM SAVE PAYLOAD", payload);
    toast.info("Line item changes captured locally.");
  }

  return (
    <Card className="card-elevated">
      <CardHeader className="flex flex-row items-center justify-between gap-3 space-y-0">
        <CardTitle className="text-base">Line items</CardTitle>
        <div className="flex gap-2">
          <Button size="sm" variant="outline" className="gap-2" onClick={addRow}>
            <Plus className="h-3.5 w-3.5" /> Add line
          </Button>
          <Button size="sm" className="gap-2" onClick={handleSaveAll}>
            <Save className="h-3.5 w-3.5" /> Save line item changes
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <LineItemsReextractDiagnosticBanner
          diagnostic={reextractDiagnostic}
          currency={currency}
        />
        {items.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No line items found for this invoice.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-surface-muted/60">
                <tr className="border-b text-left text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                  <th className="px-2 py-2">SKU</th>
                  <th className="px-2 py-2">Description</th>
                  <th className="px-2 py-2 text-right">Qty</th>
                  <th className="px-2 py-2">UoM</th>
                  <th className="px-2 py-2 text-right">Unit price</th>
                  <th className="px-2 py-2 text-right">Discount</th>
                  <th className="px-2 py-2 text-right">Tax</th>
                  <th className="px-2 py-2 text-right">Line total</th>
                  <th className="px-2 py-2 text-right">Conf.</th>
                  <th className="px-2 py-2"></th>
                </tr>
              </thead>
              <tbody>
                {items.map((it, idx) => {
                  const lineTotal = toNum(it.line_total) ?? toNum(it.amount);
                  const conf = it.confidence_score;
                  return (
                    <tr key={idx} className="border-b last:border-0 align-top">
                      <td className="px-2 py-1.5">
                        <Input
                          className="h-8"
                          value={String(it.supplier_stock_number ?? "")}
                          onChange={(e) =>
                            update(idx, "supplier_stock_number", e.target.value || null)
                          }
                        />
                      </td>
                      <td className="px-2 py-1.5 min-w-[220px]">
                        <Input
                          className="h-8"
                          value={String(it.description ?? "")}
                          onChange={(e) => update(idx, "description", e.target.value)}
                        />
                      </td>
                      <td className="px-2 py-1.5">
                        <Input
                          className="h-8 text-right tabular-nums"
                          inputMode="decimal"
                          value={it.quantity == null ? "" : String(it.quantity)}
                          onChange={(e) => update(idx, "quantity", e.target.value)}
                        />
                      </td>
                      <td className="px-2 py-1.5 w-[80px]">
                        <Input
                          className="h-8"
                          value={String(it.unit_of_measure ?? "")}
                          onChange={(e) =>
                            update(idx, "unit_of_measure", e.target.value || null)
                          }
                          placeholder="ea"
                        />
                      </td>
                      <td className="px-2 py-1.5">
                        <Input
                          className="h-8 text-right tabular-nums"
                          inputMode="decimal"
                          value={it.unit_price == null ? "" : String(it.unit_price)}
                          onChange={(e) => update(idx, "unit_price", e.target.value)}
                        />
                      </td>
                      <td className="px-2 py-1.5">
                        <Input
                          className="h-8 text-right tabular-nums"
                          inputMode="decimal"
                          value={it.discount == null ? "" : String(it.discount)}
                          onChange={(e) => update(idx, "discount", e.target.value)}
                        />
                      </td>
                      <td className="px-2 py-1.5">
                        <Input
                          className="h-8 text-right tabular-nums"
                          inputMode="decimal"
                          value={it.tax_amount == null ? "" : String(it.tax_amount)}
                          onChange={(e) => update(idx, "tax_amount", e.target.value)}
                        />
                      </td>
                      <td className="px-2 py-1.5">
                        <Input
                          className="h-8 text-right tabular-nums"
                          inputMode="decimal"
                          value={
                            it.line_total != null
                              ? String(it.line_total)
                              : it.amount != null
                                ? String(it.amount)
                                : ""
                          }
                          onChange={(e) => update(idx, "line_total", e.target.value)}
                        />
                        {lineTotal != null && (
                          <div className="mt-0.5 text-right text-[10px] text-muted-foreground tabular-nums">
                            {fmtMoney(lineTotal, currency)}
                          </div>
                        )}
                      </td>
                      <td className="px-2 py-1.5 text-right text-xs tabular-nums">
                        {conf != null ? `${Math.round(conf * 100)}%` : "—"}
                      </td>
                      <td className="px-2 py-1.5 text-right">
                        <Button
                          size="icon"
                          variant="ghost"
                          className="h-7 w-7"
                          onClick={() => removeRow(idx)}
                          aria-label="Remove line"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
              <tfoot>
                <tr className="border-t bg-surface-muted/40">
                  <td colSpan={7} className="px-2 py-2 text-right text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Sum of line totals
                  </td>
                  <td className="px-2 py-2 text-right text-sm font-semibold tabular-nums">
                    {fmtMoney(reconciliation.sum, currency)}
                  </td>
                  <td colSpan={2} />
                </tr>
                {subtotal != null && (
                  <tr className="bg-surface-muted/40">
                    <td colSpan={7} className="px-2 py-2 text-right text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                      Invoice subtotal
                    </td>
                    <td className="px-2 py-2 text-right text-sm font-semibold tabular-nums">
                      {fmtMoney(subtotal, currency)}
                    </td>
                    <td colSpan={2} />
                  </tr>
                )}
              </tfoot>
            </table>
          </div>
        )}

        {items.length > 0 && reconciliation.matches === true && (
          <BankingAlert
            tone="success"
            title="Line items agree to subtotal."
          />
        )}
        {items.length > 0 && reconciliation.matches === false && (
          <BankingAlert
            tone="destructive"
            title="Line item total does not agree to subtotal."
            detail={`Sum of lines: ${fmtMoney(reconciliation.sum, currency)} · Subtotal: ${fmtMoney(reconciliation.subtotal as number, currency)} · Diff: ${fmtMoney(reconciliation.diff as number, currency)}`}
          />
        )}
        {items.length > 0 && reconciliation.matches === null && (
          <p className="text-xs text-muted-foreground">
            No subtotal available to reconcile against.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function auditEvents(row: AnyRec): { label: string; detail: string }[] {
  const events: { label: string; detail: string }[] = [];
  const created = pickStr(row, ["created_at", "captured_at", "inserted_at"]);
  const updated = pickStr(row, ["updated_at", "modified_at"]);
  const reviewed = pickStr(row, ["reviewed_at", "approved_at"]);
  if (created) events.push({ label: "Captured", detail: fmtDateTime(created) });
  if (updated && updated !== created)
    events.push({ label: "Updated", detail: fmtDateTime(updated) });
  if (reviewed)
    events.push({ label: "Reviewed", detail: fmtDateTime(reviewed) });
  if (events.length === 0)
    events.push({
      label: "No audit history",
      detail: "Activity will appear once the invoice is updated.",
    });
  return events;
}

function fmtMoney(n: number, currency: string) {
  try {
    return n.toLocaleString(undefined, { style: "currency", currency });
  } catch {
    return `${currency} ${n.toFixed(2)}`;
  }
}
function formatConfidenceChange(before: number | null, after: number | null): string {
  if (before == null && after == null) return "—";
  if (before == null) return `After ${formatConfidence(after as number)}`;
  if (after == null) return `Before ${formatConfidence(before)}`;
  return `${formatConfidence(before)} → ${formatConfidence(after)}`;
}

function formatConfidence(value: number): string {
  const normalized = value <= 1 ? value * 100 : value;
  return `${Math.round(normalized)}%`;
}

function formatJsonValue(value: unknown): string {
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2);
}

function lineItemsDiagnosticMessage(
  diagnostic: LineItemsReextractDiagnostic | null | undefined,
  currency: string,
): { tone: "success" | "warning" | "muted"; text: string } | null {
  if (!diagnostic) return null;
  if (diagnostic.line_items_insert_error) {
    return {
      tone: "warning",
      text: `Line item save warning: ${diagnostic.line_items_insert_error}`,
    };
  }

  const found = diagnostic.line_items_found_count;
  const inserted = diagnostic.line_items_inserted_count ?? 0;
  if ((found ?? 0) > 0 && inserted > 0) {
    const total =
      diagnostic.line_items_total != null
        ? fmtMoney(diagnostic.line_items_total, currency)
        : "unknown";
    const matches = diagnostic.line_items_match_invoice_total ? "Yes" : "No";
    return {
      tone: "success",
      text: `Line items extracted: ${inserted}. Total ${total}. Matches invoice total: ${matches}.`,
    };
  }
  if ((found ?? 0) > 0 && inserted === 0) {
    return {
      tone: "warning",
      text: "Line items were detected but not saved.",
    };
  }
  if (found === 0) {
    return {
      tone: "muted",
      text: "No line items detected during re-extract.",
    };
  }
  return null;
}

function parseLineItemsReextractDiagnostic(
  payload: Record<string, unknown>,
): LineItemsReextractDiagnostic | null {
  const keys = [
    "line_items_found_count",
    "line_items_inserted_count",
    "line_items_insert_error",
    "line_items_total",
    "invoice_total",
    "line_items_match_invoice_total",
  ];
  if (!keys.some((key) => Object.prototype.hasOwnProperty.call(payload, key))) {
    return null;
  }

  return {
    line_items_found_count: readDiagnosticNumber(payload.line_items_found_count),
    line_items_inserted_count: readDiagnosticNumber(payload.line_items_inserted_count),
    line_items_insert_error: readDiagnosticString(payload.line_items_insert_error),
    line_items_total: readDiagnosticNumber(payload.line_items_total),
    invoice_total: readDiagnosticNumber(payload.invoice_total),
    line_items_match_invoice_total: readDiagnosticBool(
      payload.line_items_match_invoice_total,
    ),
  };
}

function readDiagnosticNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed && !Number.isNaN(Number(trimmed))) return Number(trimmed);
  }
  return null;
}

function readDiagnosticString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function readDiagnosticBool(value: unknown): boolean | null {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") {
    const normalised = value.trim().toLowerCase();
    if (normalised === "true" || normalised === "yes") return true;
    if (normalised === "false" || normalised === "no") return false;
  }
  return null;
}

function lineItemsDiagnosticStorageKey(invoiceId: string) {
  return `apflow:line-items-reextract-diagnostic:${invoiceId}`;
}

function readStoredLineItemsDiagnostic(invoiceId: string): LineItemsReextractDiagnostic | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(lineItemsDiagnosticStorageKey(invoiceId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return parseLineItemsReextractDiagnostic(parsed);
  } catch {
    return null;
  }
}

function storeLineItemsDiagnostic(
  invoiceId: string,
  diagnostic: LineItemsReextractDiagnostic | null,
) {
  if (typeof window === "undefined") return;
  const key = lineItemsDiagnosticStorageKey(invoiceId);
  if (!diagnostic) {
    window.sessionStorage.removeItem(key);
    return;
  }
  window.sessionStorage.setItem(key, JSON.stringify(diagnostic));
}

function fmtDate(s: string | null) {
  if (!s) return "—";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? s : d.toLocaleDateString();
}
function fmtDateTime(s: string) {
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString();
}
function normaliseAcct(s: string) {
  return s.replace(/\s+/g, "").toLowerCase();
}
function inferCountryFromCurrency(currency?: string | null): string | null {
  if (!currency) return null;
  const map: Record<string, string> = {
    USD: "US", CAD: "CA", GBP: "GB", EUR: "EU", AUD: "AU", NZD: "NZ",
    ZAR: "ZA", JPY: "JP", CNY: "CN", INR: "IN", CHF: "CH", SEK: "SE",
    NOK: "NO", DKK: "DK", SGD: "SG", HKD: "HK", AED: "AE",
  };
  return map[currency.toUpperCase()] ?? null;
}
function pickStr(
  r: AnyRec | null | undefined,
  keys: string[],
): string | null {
  if (!r) return null;
  for (const k of keys) {
    const v = r[k];
    if (typeof v === "string" && v.length > 0) return v;
    if (typeof v === "number") return String(v);
  }
  return null;
}
function pickNum(
  r: AnyRec | null | undefined,
  keys: string[],
): number | null {
  if (!r) return null;
  for (const k of keys) {
    const v = r[k];
    if (typeof v === "number") return v;
    if (typeof v === "string" && v && !Number.isNaN(Number(v)))
      return Number(v);
  }
  return null;
}

type SupplierExtractionField = {
  key: string;
  label: string;
  value: string;
};

const SUPPLIER_PROFILE_FIELDS: Array<[string, string]> = [
  ["supplier_name", "Supplier name"],
  ["supplier_code", "Supplier code"],
  ["account_number", "Account number"],
  ["tax_number", "Tax number"],
  ["registration_number", "Registration number"],
  ["currency", "Currency"],
  ["default_email", "Default email"],
  ["phone", "Phone"],
  ["payment_terms", "Payment terms"],
  ["vat_number", "VAT number"],
  ["company_registration_number", "Company registration number"],
  ["payment_terms_text", "Payment terms text"],
  ["payment_terms_days", "Payment terms days"],
  ["early_settlement_discount_percent", "Early settlement discount"],
  ["early_settlement_days", "Early settlement days"],
  ["bank_account_name", "Bank account name"],
  ["bank_name", "Bank name"],
  ["bank_account_number", "Bank account number"],
  ["bank_branch_code", "Bank branch code"],
  ["bank_swift_code", "Bank SWIFT code"],
  ["bank_country", "Bank country"],
  ["bank_verified", "Bank verified"],
  ["bank_details_last_updated_at", "Bank details last updated"],
  ["bank_details_source", "Bank details source"],
];

const SUPPLIER_DRAFT_SKIP = new Set([
  "organisation_id",
  "active",
  "bank_verified",
  "bank_details_last_updated_at",
  "bank_details_source",
  "created_at",
  "updated_at",
]);

const SUPPLIER_NUMERIC_FIELDS = new Set([
  "payment_terms",
  "payment_terms_days",
  "early_settlement_discount_percent",
  "early_settlement_days",
]);

const SUPPLIER_FIELD_ALLOW = [
  "supplier",
  "vendor",
  "merchant",
  "company",
  "business",
  "trading",
  "contact",
  "address",
  "email",
  "fax",
  "phone",
  "telephone",
  "mobile",
  "website",
  "web",
  "url",
  "domain",
  "vat",
  "tax",
  "registration",
  "reg",
  "number",
  "abn",
  "acn",
  "gst",
  "trn",
];

const SUPPLIER_FIELD_BLOCK = [
  "invoice",
  "receipt",
  "statement",
  "date",
  "due",
  "total",
  "subtotal",
  "amount",
  "balance",
  "currency",
  "line",
  "item",
  "quantity",
  "price",
  "discount",
  "confidence",
  "status",
  "raw",
  "parsed",
  "bank",
  "sort",
  "swift",
  "iban",
  "payment",
];

function supplierExtractionFields(parsed: AnyRec, row: AnyRec | null | undefined): SupplierExtractionField[] {
  const profile = pickSupplierProfile(parsed, row);
  if (profile) {
    return supplierProfileFields(profile);
  }

  const roots: Array<{ prefix: string; value: unknown }> = [
    { prefix: "", value: parsed },
    { prefix: "", value: row ?? {} },
  ];
  const fields = new Map<string, SupplierExtractionField>();

  for (const root of roots) {
    collectSupplierExtractionFields(root.value, root.prefix, fields);
  }

  return Array.from(fields.values()).sort((a, b) => a.label.localeCompare(b.label));
}

function pickSupplierProfile(parsed: AnyRec, row: AnyRec | null | undefined): AnyRec | null {
  const candidates = [
    parsed.extracted_supplier_profile,
    parsed.supplier_profile,
    parsed.supplierProfile,
    row?.extracted_supplier_profile,
    row?.supplier_profile,
  ];

  for (const candidate of candidates) {
    if (candidate && typeof candidate === "object" && !Array.isArray(candidate)) {
      return candidate as AnyRec;
    }
  }

  return null;
}

function pickSupplierCreatePayload(parsed: AnyRec, row: AnyRec | null | undefined): Record<string, unknown> | null {
  const candidates = [
    parsed.supplier_create_payload,
    parsed.supplierCreatePayload,
    parsed.supplier_payload,
    row?.supplier_create_payload,
    row?.supplierCreatePayload,
    row?.supplier_payload,
  ];

  for (const candidate of candidates) {
    if (candidate && typeof candidate === "object" && !Array.isArray(candidate)) {
      return candidate as Record<string, unknown>;
    }
  }

  return null;
}

function supplierProfileFields(profile: AnyRec): SupplierExtractionField[] {
  const fields = new Map<string, SupplierExtractionField>();

  for (const [key, label] of SUPPLIER_PROFILE_FIELDS) {
    const value = formatSupplierProfileValue(profile[key]);
    if (!value) continue;
    fields.set(key, { key, label, value });
  }

  for (const [key, rawValue] of Object.entries(profile)) {
    if (fields.has(key)) continue;
    if (key === "organisation_id" || key === "created_at" || key === "updated_at") continue;
    const value = formatSupplierProfileValue(rawValue);
    if (!value) continue;
    fields.set(key, {
      key,
      label: supplierFieldLabel(key),
      value,
    });
  }

  return Array.from(fields.values());
}

function formatSupplierProfileValue(value: unknown): string | null {
  if (value == null || value === "") return null;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value.trim() || null;
  if (Array.isArray(value)) {
    const joined = value
      .map((item) => formatSupplierProfileValue(item))
      .filter(Boolean)
      .join(", ");
    return joined || null;
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

function buildSupplierCreatePayload({
  profile,
  row,
  parsed,
  organisationId,
  fallbackCurrency,
  fallbackBanking,
}: {
  profile: AnyRec | null;
  row: AnyRec | null | undefined;
  parsed: AnyRec;
  organisationId: string;
  fallbackCurrency: string;
  fallbackBanking: Record<string, string | null>;
}): Record<string, unknown> {
  const p = profile ?? {};
  const supplierName =
    supplierProfileString(p, ["supplier_name", "name", "supplier", "vendor", "trading_name"]) ||
    pickStr(row, ["supplier_name_extracted", "supplier_name", "supplier", "vendor"]) ||
    pickStr(parsed, ["supplier_name", "supplier", "vendor"]) ||
    "";
  const payload: Record<string, unknown> = {
    organisation_id: organisationId,
    supplier_name: supplierName.trim(),
    active: true,
  };

  setPayloadString(payload, "supplier_code", p, ["supplier_code", "code"]);
  setPayloadString(payload, "account_number", p, [
    "account_number",
    "account_no",
    "account_num",
    "supplier_account_number",
    "supplier_acc_no",
  ]);
  setPayloadString(payload, "tax_number", p, ["tax_number", "tax_no", "tax_id"]);
  setPayloadString(payload, "registration_number", p, [
    "registration_number",
    "reg_number",
    "reg_no",
    "company_reg_no",
  ]);
  setPayloadString(payload, "currency", p, ["currency", "currency_code"]);
  if (!payload.currency && fallbackCurrency) payload.currency = fallbackCurrency;
  setPayloadString(payload, "default_email", p, [
    "default_email",
    "email",
    "supplier_email",
    "supplier_acc_email",
    "accounts_email",
  ]);
  setPayloadString(payload, "phone", p, ["phone", "telephone", "supplier_telephone", "tel"]);
  setPayloadNumber(payload, "payment_terms", p, ["payment_terms", "payment_terms_days"]);
  setPayloadString(payload, "vat_number", p, [
    "vat_number",
    "vat_no",
    "vat_registration_number",
    "vat_registration_no",
    "vat_reg_no",
  ]);
  setPayloadString(payload, "company_registration_number", p, [
    "company_registration_number",
    "company_registration_no",
    "company_reg_no",
  ]);
  setPayloadString(payload, "payment_terms_text", p, ["payment_terms_text", "terms"]);
  setPayloadNumber(payload, "payment_terms_days", p, ["payment_terms_days"]);
  setPayloadNumber(payload, "early_settlement_discount_percent", p, [
    "early_settlement_discount_percent",
    "early_settlement_discount",
  ]);
  setPayloadNumber(payload, "early_settlement_days", p, ["early_settlement_days"]);

  for (const key of [
    "bank_account_name",
    "bank_name",
    "bank_account_number",
    "bank_branch_code",
    "bank_swift_code",
    "bank_country",
  ]) {
    setPayloadString(payload, key, p, [key]);
    if (!payload[key] && fallbackBanking[key]) payload[key] = fallbackBanking[key];
  }

  const hasBankDetails = [
    "bank_account_name",
    "bank_name",
    "bank_account_number",
    "bank_branch_code",
    "bank_swift_code",
    "bank_country",
  ].some((key) => Boolean(payload[key]));
  if (hasBankDetails) {
    payload.bank_verified = false;
    payload.bank_details_source = "invoice_extraction";
    payload.bank_details_last_updated_at = new Date().toISOString();
  }

  Object.keys(payload).forEach((key) => {
    if (payload[key] === null || payload[key] === "") delete payload[key];
  });

  return payload;
}

function supplierDraftFromPayload(payload: Record<string, unknown>): Record<string, string> {
  const draft: Record<string, string> = {};

  for (const [key] of SUPPLIER_PROFILE_FIELDS) {
    if (SUPPLIER_DRAFT_SKIP.has(key)) continue;
    const value = formatSupplierProfileValue(payload[key]);
    draft[key] = value ?? "";
  }

  return draft;
}

function supplierDraftDisplayFields(draft: Record<string, string>): SupplierExtractionField[] {
  const fields: SupplierExtractionField[] = [];

  for (const [key, label] of SUPPLIER_PROFILE_FIELDS) {
    if (SUPPLIER_DRAFT_SKIP.has(key)) continue;
    fields.push({
      key,
      label,
      value: draft[key] ?? "",
    });
  }

  return fields;
}

function supplierPayloadFromDraft(
  draft: Record<string, string>,
  organisationId: string,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    organisation_id: organisationId,
    active: true,
  };

  for (const [key] of SUPPLIER_PROFILE_FIELDS) {
    if (SUPPLIER_DRAFT_SKIP.has(key)) continue;
    const raw = (draft[key] ?? "").trim();
    if (!raw) continue;
    payload[key] = SUPPLIER_NUMERIC_FIELDS.has(key) ? Number(raw) : raw;
  }

  const hasBankDetails = [
    "bank_account_name",
    "bank_name",
    "bank_account_number",
    "bank_branch_code",
    "bank_swift_code",
    "bank_country",
  ].some((key) => Boolean(payload[key]));

  if (hasBankDetails) {
    payload.bank_verified = false;
    payload.bank_details_source = "invoice_extraction";
    payload.bank_details_last_updated_at = new Date().toISOString();
  }

  return payload;
}

function supplierProfileString(profile: AnyRec, keys: string[]): string | null {
  for (const key of keys) {
    const value = profile[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number") return String(value);
  }
  return null;
}

function supplierProfileNumber(profile: AnyRec, keys: string[]): number | null {
  for (const key of keys) {
    const value = profile[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() && !Number.isNaN(Number(value))) {
      return Number(value);
    }
  }
  return null;
}

function setPayloadString(
  payload: Record<string, unknown>,
  target: string,
  profile: AnyRec,
  keys: string[],
) {
  const value = supplierProfileString(profile, keys);
  if (value) payload[target] = value;
}

function setPayloadNumber(
  payload: Record<string, unknown>,
  target: string,
  profile: AnyRec,
  keys: string[],
) {
  const value = supplierProfileNumber(profile, keys);
  if (value != null) payload[target] = value;
}

function collectSupplierExtractionFields(
  value: unknown,
  path: string,
  fields: Map<string, SupplierExtractionField>,
) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return;

  for (const [key, rawValue] of Object.entries(value as AnyRec)) {
    const nextPath = path ? `${path}.${key}` : key;
    if (rawValue == null || rawValue === "") continue;
    if (Array.isArray(rawValue)) continue;

    if (typeof rawValue === "object") {
      if (isSupplierContextKey(nextPath)) {
        collectSupplierExtractionFields(rawValue, nextPath, fields);
      }
      continue;
    }

    if (!isSupplierExtractionKey(nextPath)) continue;

    const text = String(rawValue).trim();
    if (!text) continue;

    const normalizedKey = normalizedFieldKey(nextPath);
    if (!fields.has(normalizedKey)) {
      fields.set(normalizedKey, {
        key: normalizedKey,
        label: supplierFieldLabel(nextPath),
        value: text,
      });
    }
  }
}

function isSupplierContextKey(path: string): boolean {
  const normalized = path.toLowerCase();
  return ["supplier", "vendor", "merchant", "company", "business", "contact"].some((token) =>
    normalized.includes(token),
  );
}

function isSupplierExtractionKey(path: string): boolean {
  const normalized = path.toLowerCase();
  const parts = normalized.split(/[._\s-]+/).filter(Boolean);
  const leaf = parts[parts.length - 1] ?? normalized;
  if (leaf === "id" || leaf === "uuid") return false;
  if (isBankingField(parts)) return false;
  const isExplicitSupplierValue = ["name", "legal", "legalname", "tradingname"].includes(leaf) &&
    isSupplierContextKey(normalized);
  const isAccountReference = parts.includes("account") &&
    parts.some((part) => part === "no" || part === "num" || part === "number");
  const allowed = isExplicitSupplierValue || SUPPLIER_FIELD_ALLOW.some((token) =>
    parts.some((part) => part === token || part.includes(token)),
  ) || isAccountReference;
  if (!allowed) return false;

  return !SUPPLIER_FIELD_BLOCK.some((token) =>
    parts.some((part) => part === token || part.includes(token)),
  );
}

function isBankingField(parts: string[]): boolean {
  const hasAccount = parts.includes("account");
  const hasBankToken = parts.some((part) =>
    ["bank", "iban", "swift", "bic", "branch", "routing", "sort"].includes(part),
  );
  return hasBankToken || (hasAccount && parts.some((part) => ["holder", "name"].includes(part)));
}

function normalizedFieldKey(path: string): string {
  return path
    .replace(/_extracted$/i, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

function supplierFieldLabel(path: string): string {
  const leaf = path.split(".").pop() ?? path;
  const withoutSuffix = leaf.replace(/_extracted$/i, "");
  const words = withoutSuffix
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .trim();
  if (!words) return "Supplier field";

  return words.replace(/\b\w/g, (m) => m.toUpperCase());
}

function humanEventLabel(eventType: string): string {
  switch (eventType) {
    case "banking_override_saved":
      return "Banking overrides saved";
    case "review_status_changed":
      return "Review status changed";
    default:
      return eventType.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }
}

function EditBankingDialog({
  open,
  onOpenChange,
  saving,
  extracted,
  current,
  onSave,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  saving: boolean;
  extracted: {
    bank_account_number: string | null;
    bank_name: string | null;
    sort_code: string | null;
  };
  current: {
    bank_account_number: string | null;
    bank_name: string | null;
    sort_code: string | null;
  };
  onSave: (values: {
    override_bank_account_number: string | null;
    override_bank_name: string | null;
    override_sort_code: string | null;
  }) => void | Promise<void>;
}) {
  const [acct, setAcct] = useState(current.bank_account_number ?? "");
  const [name, setName] = useState(current.bank_name ?? "");
  const [sort, setSort] = useState(current.sort_code ?? "");

  useEffect(() => {
    if (open) {
      setAcct(current.bank_account_number ?? "");
      setName(current.bank_name ?? "");
      setSort(current.sort_code ?? "");
    }
  }, [open, current.bank_account_number, current.bank_name, current.sort_code]);

  const norm = (v: string | null) => (v ?? "").trim();
  const hasChanges =
    norm(acct) !== norm(current.bank_account_number) ||
    norm(name) !== norm(current.bank_name) ||
    norm(sort) !== norm(current.sort_code);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!hasChanges) return;
    void onSave({
      override_bank_account_number: acct.trim() ? acct.trim() : null,
      override_bank_name: name.trim() ? name.trim() : null,
      override_sort_code: sort.trim() ? sort.trim() : null,
    });
  }

  function handleClear() {
    setAcct("");
    setName("");
    setSort("");
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Edit banking details</DialogTitle>
          <DialogDescription>
            Extracted values are read-only. Provide override values to correct
            the data used for validation and payment. Leave blank to fall back
            to the extracted value.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <OverrideRow
            label="Bank name"
            originalValue={extracted.bank_name}
            value={name}
            onChange={setName}
            isOverride={!!name.trim()}
          />
          <OverrideRow
            label="Account number"
            originalValue={extracted.bank_account_number}
            value={acct}
            onChange={setAcct}
            mono
            isOverride={!!acct.trim()}
          />
          <OverrideRow
            label="Sort / routing code"
            originalValue={extracted.sort_code}
            value={sort}
            onChange={setSort}
            mono
            isOverride={!!sort.trim()}
          />
          <DialogFooter className="gap-2 sm:justify-between">
            <Button
              type="button"
              variant="ghost"
              onClick={handleClear}
              disabled={saving}
            >
              Clear overrides
            </Button>
            <div className="flex gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={saving}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={saving || !hasChanges}>
                {saving ? "Saving…" : "Save overrides"}
              </Button>
            </div>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function OverrideRow({
  label,
  originalValue,
  value,
  onChange,
  mono,
  isOverride,
}: {
  label: string;
  originalValue: string | null;
  value: string;
  onChange: (v: string) => void;
  mono?: boolean;
  isOverride?: boolean;
}) {
  const id = `override-${label.replace(/\W+/g, "-").toLowerCase()}`;
  return (
    <div className="grid gap-1.5">
      <Label
        htmlFor={id}
        className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground"
      >
        <span>{label}</span>
        {isOverride && (
          <span className="rounded-full bg-primary/15 px-1.5 py-0.5 text-[9px] font-semibold tracking-normal text-primary">
            Override active
          </span>
        )}
      </Label>
      <div className="rounded-md border border-dashed bg-surface-muted/30 px-3 py-1.5 text-xs">
        <span className="text-muted-foreground">Original: </span>
        <span className={mono ? "font-mono" : ""}>{originalValue || "—"}</span>
      </div>
      <Input
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Override (leave blank to use original)"
        className={`${mono ? "font-mono " : ""}${isOverride ? "border-primary/50 bg-primary/5 focus-visible:ring-primary" : ""}`}
        autoComplete="off"
      />
    </div>
  );
}

function LinkSupplierDialog({
  open,
  onOpenChange,
  organisationId,
  suggestedName,
  saving,
  onSelect,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  organisationId: string | null;
  suggestedName: string;
  saving: boolean;
  onSelect: (id: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [rows, setRows] = useState<AnyRec[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    setSearch(suggestedName ?? "");
  }, [open, suggestedName]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    (async () => {
      let q = supabase.from("suppliers").select("*").limit(50);
      if (organisationId) q = q.eq("organisation_id", organisationId);
      const term = search.trim();
      if (term) {
        q = q.or(
          `supplier_name.ilike.%${term}%,name.ilike.%${term}%,trading_name.ilike.%${term}%`,
        );
      }
      const { data, error } = await q;
      if (cancelled) return;
      if (error) {
        const fb = await supabase.from("suppliers").select("*").limit(50);
        setRows((fb.data as AnyRec[]) ?? []);
      } else {
        setRows((data as AnyRec[]) ?? []);
      }
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [open, search, organisationId]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Link existing supplier</DialogTitle>
          <DialogDescription>
            Search and select a supplier from your organisation.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <Input
            autoFocus
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search suppliers…"
          />
          <div className="max-h-72 overflow-y-auto rounded-md border">
            {loading ? (
              <div className="p-4 text-sm text-muted-foreground">Loading…</div>
            ) : rows.length === 0 ? (
              <div className="p-4 text-sm text-muted-foreground">
                No suppliers found.
              </div>
            ) : (
              <ul className="divide-y">
                {rows.map((r) => {
                  const name =
                    pickStr(r, ["supplier_name", "name", "trading_name"]) || "—";
                  const ident =
                    pickStr(r, ["vat_number", "vat", "tax_id", "abn", "code"]) || "";
                  return (
                    <li key={r.id as string}>
                      <button
                        type="button"
                        disabled={saving}
                        onClick={() => onSelect(r.id as string)}
                        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm hover:bg-accent disabled:opacity-50"
                      >
                        <span className="truncate font-medium">{name}</span>
                        {ident && (
                          <span className="font-mono text-xs text-muted-foreground">
                            {ident}
                          </span>
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
            Cancel
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
