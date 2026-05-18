import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, FileUp, Loader2, Play } from "lucide-react";
import { toast } from "sonner";
import { PageHeader } from "@/components/app/PageHeader";
import { EmptyState } from "@/components/app/EmptyState";
import { StatusBadge } from "@/components/app/StatusBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { supabase } from "@/integrations/supabase/client";
import { useOrg } from "@/lib/org";
import { useAuth } from "@/lib/auth";
import {
  buildStoragePath,
  callExtraction,
  insertRowWithFallback,
  pickString,
  storageBucket,
  updateRowWithFallback,
} from "@/lib/ingestion";

export const Route = createFileRoute("/_app/invoices/upload")({
  component: InvoiceUploadPage,
});

type Row = Record<string, unknown> & { id: string };

const ACCEPTED_MIME = "application/pdf,image/png,image/jpeg,image/webp,image/heic";

function inferMime(file: File) {
  if (file.type) return file.type;
  const name = file.name.toLowerCase();
  if (name.endsWith(".pdf")) return "application/pdf";
  if (name.endsWith(".png")) return "image/png";
  if (name.endsWith(".jpg") || name.endsWith(".jpeg")) return "image/jpeg";
  if (name.endsWith(".webp")) return "image/webp";
  if (name.endsWith(".heic")) return "image/heic";
  return "application/octet-stream";
}

function isBackendUnavailable(error: unknown) {
  if (!(error instanceof Error)) return false;
  const msg = error.message.toLowerCase();
  return (
    msg.includes("vite_fastapi_url is not configured") ||
    msg.includes("failed to fetch") ||
    msg.includes("networkerror") ||
    msg.includes("load failed")
  );
}

function formatErrorMessage(error: unknown): string {
  if (!error) return "Unknown error";
  if (typeof error === "string") return error;
  if (error instanceof Error) {
    const details = (error as { details?: unknown }).details;
    const hint = (error as { hint?: unknown }).hint;
    const code = (error as { code?: unknown }).code;
    const parts = [error.message];
    if (typeof details === "string" && details.trim()) parts.push(details);
    if (typeof hint === "string" && hint.trim()) parts.push(hint);
    if (typeof code === "string" && code.trim()) parts.push(`(${code})`);
    const joined = parts.filter(Boolean).join(" — ");
    return joined || error.message || "Unknown error";
  }
  if (typeof error === "object") {
    const obj = error as Record<string, unknown>;
    const message = typeof obj.message === "string" ? obj.message : null;
    const details = typeof obj.details === "string" ? obj.details : null;
    if (message || details) {
      return [message, details].filter(Boolean).join(" — ");
    }
    try {
      return JSON.stringify(error);
    } catch {
      return String(error);
    }
  }
  return String(error);
}

function InvoiceUploadPage() {
  const { currentOrgId } = useOrg();
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [file, setFile] = useState<File | null>(null);
  const [supplierId, setSupplierId] = useState<string>("unassigned");

  const suppliers = useQuery({
    queryKey: ["upload_suppliers", currentOrgId],
    enabled: Boolean(currentOrgId),
    queryFn: async () => {
      let q = supabase.from("suppliers").select("*").order("supplier_name").limit(500);
      if (currentOrgId) q = q.eq("organisation_id", currentOrgId);
      const { data, error } = await q;
      if (error) throw error;
      return (data ?? []) as Row[];
    },
  });

  const raws = useQuery({
    queryKey: ["invoices_raw", currentOrgId],
    enabled: Boolean(currentOrgId),
    refetchInterval: (query) => {
      const rows = (query.state.data ?? []) as Row[];
      const active = rows.some((r) => {
        const s = String(r.parse_status ?? "").toLowerCase();
        return s === "pending" || s === "processing";
      });
      return active ? 3000 : false;
    },
    queryFn: async () => {
      let q = supabase.from("invoices_raw").select("*").limit(200);
      if (currentOrgId) q = q.eq("organisation_id", currentOrgId);
      const { data, error } = await q.order("uploaded_at", { ascending: false, nullsFirst: false });
      if (error) throw error;
      return (data ?? []) as Row[];
    },
  });

  const extracted = useQuery({
    queryKey: ["invoices_extracted", currentOrgId, "queue_lookup"],
    enabled: Boolean(currentOrgId),
    queryFn: async () => {
      let q = supabase.from("invoices_extracted").select("*").limit(500);
      if (currentOrgId) q = q.eq("organisation_id", currentOrgId);
      const { data, error } = await q;
      if (error) throw error;
      return (data ?? []) as Row[];
    },
  });

  // Verified relationship column on invoices_extracted is `invoice_raw_id`.
  const EXTRACTED_RAW_FK = "invoice_raw_id" as const;
  const extractedByRaw = useMemo(() => {
    const map = new Map<string, Row>();
    for (const row of extracted.data ?? []) {
      const rawId = row[EXTRACTED_RAW_FK] as string | undefined;
      if (rawId) map.set(String(rawId), row);
    }
    // eslint-disable-next-line no-console
    console.log("EXTRACTED LOOKUP FILTER", {
      column: EXTRACTED_RAW_FK,
      mappedRawIds: Array.from(map.keys()),
    });
    return map;
  }, [extracted.data]);

  const supplierMap = useMemo(() => {
    const map = new Map<string, Row>();
    for (const row of suppliers.data ?? []) map.set(String(row.id), row);
    return map;
  }, [suppliers.data]);

  const uploadMutation = useMutation({
    mutationFn: async () => {
      if (!currentOrgId) throw new Error("Select an organisation first");
      if (!file) throw new Error("Choose an invoice or receipt file first");
      const mime = inferMime(file);
      const path = buildStoragePath("invoice", currentOrgId, file.name);
      const bucket = storageBucket("invoice");

      // ---- Stage 1: Supabase Storage upload ----
      try {
        // eslint-disable-next-line no-console
        console.info("[invoice upload] storage upload starting", {
          bucket,
          path,
          file_name: file.name,
          mime,
          size: file.size,
        });
        const { error: uploadError } = await supabase.storage.from(bucket).upload(path, file, {
          contentType: mime,
          upsert: false,
        });
        if (uploadError) {
          // eslint-disable-next-line no-console
          console.error("[invoice upload] storage upload failed", {
            bucket,
            path,
            file_name: file.name,
            mime,
            size: file.size,
            error: {
              name: (uploadError as { name?: string }).name,
              message: uploadError.message,
              statusCode: (uploadError as { statusCode?: string | number }).statusCode,
              raw: uploadError,
            },
          });
          const wrapped = new Error(`[upload:storage] ${uploadError.message}`) as Error & {
            cause?: unknown;
            bucket?: string;
            path?: string;
            stage?: string;
          };
          wrapped.cause = uploadError;
          wrapped.bucket = bucket;
          wrapped.path = path;
          wrapped.stage = "storage";
          throw wrapped;
        }
      } catch (err) {
        if (err instanceof Error && err.message.startsWith("[upload:storage]")) throw err;
        // eslint-disable-next-line no-console
        console.error("[invoice upload] storage upload threw", { bucket, path, err });
        const wrapped = new Error(
          `[upload:storage] ${err instanceof Error ? err.message : String(err)}`,
        ) as Error & { cause?: unknown; bucket?: string; path?: string; stage?: string };
        wrapped.cause = err;
        wrapped.bucket = bucket;
        wrapped.path = path;
        wrapped.stage = "storage";
        throw wrapped;
      }

      // ---- Stage 2: invoices_raw insert ----
      const supplier = supplierId !== "unassigned" ? supplierMap.get(supplierId) : null;
      const nowIso = new Date().toISOString();
      const payload = {
        organisation_id: currentOrgId,
        supplier_id: supplier?.id ?? null,
        file_name: file.name,
        file_path: path,
        file_type: mime,
        source_type: "upload",
        upload_status: "uploaded",
        parse_status: "pending",
        uploaded_by: user?.id ?? null,
        uploaded_at: nowIso,
      };

      try {
        // eslint-disable-next-line no-console
        console.info("[invoice upload] invoices_raw insert starting", { payload });
        await insertRowWithFallback("invoices_raw", payload);
      } catch (err) {
        const sbError = err as {
          message?: string;
          code?: string;
          details?: string;
          hint?: string;
        };
        // eslint-disable-next-line no-console
        console.error("[invoice upload] invoices_raw insert failed", {
          payload,
          error: {
            message: sbError?.message,
            code: sbError?.code,
            details: sbError?.details,
            hint: sbError?.hint,
            raw: err,
          },
        });
        const wrapped = new Error(
          `[upload:db_insert] ${err instanceof Error ? err.message : String(err)}`,
        ) as Error & { cause?: unknown; payload?: unknown; stage?: string };
        wrapped.cause = err;
        wrapped.payload = payload;
        wrapped.stage = "db_insert";
        throw wrapped;
      }
    },
    onSuccess: async () => {
      toast.success("File uploaded");
      setFile(null);
      const input = document.getElementById("invoice-file-input") as HTMLInputElement | null;
      if (input) input.value = "";
      await queryClient.invalidateQueries({ queryKey: ["invoices_raw", currentOrgId] });
    },
    onError: (error) => {
      // eslint-disable-next-line no-console
      console.error("[invoice upload] failure", error);
      const message = error instanceof Error ? error.message : "Upload failed";
      const stage = (error as { stage?: string })?.stage;
      const friendly = stage
        ? `Upload failed (${stage}): ${message.replace(/^\[upload:[^\]]+\]\s*/, "")}`
        : message;
      toast.error(friendly);
    },
  });

  const extractMutation = useMutation({
    mutationFn: async (raw: Row) => {
      if (!currentOrgId) throw new Error("Select an organisation first");

      await updateRowWithFallback("invoices_raw", String(raw.id), {
        parse_status: "processing",
      });
      await queryClient.invalidateQueries({ queryKey: ["invoices_raw", currentOrgId] });

      // Backend now persists invoices_extracted itself. Frontend only sends
      // the raw id + organisation and consumes the response.
      const extractRequest = {
        invoice_raw_id: raw.id,
        organisation_id: currentOrgId,
      };
      // eslint-disable-next-line no-console
      console.log("EXTRACT REQUEST PAYLOAD", extractRequest);

      const response = (await callExtraction(
        "/api/invoices/extract",
        extractRequest,
      )) as Record<string, unknown>;

      return response;
    },
    onSuccess: async () => {
      toast.success("Queued for extraction");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["invoices_raw", currentOrgId] }),
        queryClient.invalidateQueries({ queryKey: ["upload_suppliers", currentOrgId] }),
        queryClient.invalidateQueries({ queryKey: ["invoices_extracted", currentOrgId] }),
        queryClient.invalidateQueries({ queryKey: ["invoices", currentOrgId] }),
        queryClient.invalidateQueries({ queryKey: ["invoices"] }),
      ]);
    },
    onError: async (error, raw) => {
      const stage = (error as { stage?: string })?.stage;
      const friendly = isBackendUnavailable(error)
        ? "Extraction backend not connected yet"
        : formatErrorMessage(error) || "Extraction failed";
      toast.error(friendly);
      if (stage !== "db_save") {
        try {
          await updateRowWithFallback("invoices_raw", String(raw.id), {
            parse_status: "failed",
          });
        } catch {
          // ignore secondary errors
        }
      }
      await queryClient.invalidateQueries({ queryKey: ["invoices_raw", currentOrgId] });
    },
  });

  return (
    <>
      <PageHeader
        title="Upload Invoices & Receipts"
        description="Store supplier invoice or receipt files (PDF or image) and queue them for extraction."
      />

      <Card className="card-elevated mb-6">
        <CardHeader>
          <CardTitle className="text-base">New invoice / receipt upload</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4 md:flex-row md:items-end">
          <div className="grid flex-1 gap-2">
            <Label htmlFor="invoice-file-input">Invoice or receipt (PDF / image)</Label>
            <Input
              id="invoice-file-input"
              type="file"
              accept={ACCEPTED_MIME}
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>
          <div className="grid gap-2 md:w-[280px] md:shrink-0">
            <Label>Supplier hint (optional)</Label>
            <Select value={supplierId} onValueChange={setSupplierId}>
              <SelectTrigger>
                <SelectValue placeholder="Auto-detect supplier" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="unassigned">Auto-detect supplier</SelectItem>
                {(suppliers.data ?? []).map((supplier) => (
                  <SelectItem key={supplier.id} value={String(supplier.id)}>
                    {pickString(supplier, ["supplier_name", "name"]) ?? "Unnamed supplier"}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Leave blank for automatic supplier detection from the invoice.
            </p>
          </div>
          <Button
            className="gap-2 md:w-[140px] md:shrink-0"
            onClick={() => uploadMutation.mutate()}
            disabled={uploadMutation.isPending || !file}
          >
            {uploadMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <FileUp className="h-4 w-4" />
            )}
            Upload
          </Button>
        </CardContent>
      </Card>

      <Card className="card-elevated overflow-hidden">
        <CardHeader>
          <CardTitle className="text-base">Upload queue</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {raws.isLoading ? (
            <div className="p-8 text-sm text-muted-foreground">Loading uploads…</div>
          ) : raws.error ? (
            <div className="p-8 text-sm text-destructive">{(raws.error as Error).message}</div>
          ) : (raws.data ?? []).length === 0 ? (
            <EmptyState
              icon={FileUp}
              title="No uploads yet"
              description="Uploaded invoices and receipts will appear here."
            />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Filename</TableHead>
                  <TableHead>Supplier</TableHead>
                  <TableHead>Upload date</TableHead>
                  <TableHead>Parse status</TableHead>
                  <TableHead className="text-right">Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(raws.data ?? []).map((row) => {
                  const parseStatus =
                    pickString(row, ["parse_status", "status", "upload_status"]) || "pending";
                  const uploadedAt = pickString(row, [
                    "uploaded_at",
                    "upload_date",
                    "created_at",
                  ]);
                  const supplierName =
                    pickString(row, ["supplier_name", "supplier"]) ||
                    (row.supplier_id
                      ? pickString(supplierMap.get(String(row.supplier_id)) ?? null, [
                          "supplier_name",
                          "name",
                        ])
                      : null);
                  const isProcessing =
                    (extractMutation.isPending && extractMutation.variables?.id === row.id) ||
                    parseStatus === "queued" ||
                    parseStatus === "processing";
                  const extractedRow = extractedByRaw.get(String(row.id));
                  const isExtracted =
                    !!extractedRow ||
                    parseStatus.toLowerCase() === "completed" ||
                    parseStatus.toLowerCase() === "extracted";
                  return (
                    <TableRow key={row.id}>
                      <TableCell className="font-medium">
                        {pickString(row, ["file_name", "filename", "original_filename"]) || "—"}
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {supplierName ||
                          pickString(extractedRow ?? null, ["supplier_name"]) || (
                            <span className="italic">Pending auto-detection</span>
                          )}
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {uploadedAt ? new Date(uploadedAt).toLocaleString() : "—"}
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={isExtracted ? "extracted" : parseStatus} />
                      </TableCell>
                      <TableCell className="text-right">
                        {isExtracted ? (
                          <Link
                            to="/invoices/$invoiceId"
                            params={{
                              invoiceId: String(extractedRow?.id ?? row.id),
                            }}
                          >
                            <Button size="sm" variant="outline" className="gap-2">
                              <Eye className="h-4 w-4" />
                              Review
                            </Button>
                          </Link>
                        ) : (
                          <Button
                            size="sm"
                            variant="outline"
                            className="gap-2"
                            disabled={isProcessing}
                            onClick={() => extractMutation.mutate(row)}
                          >
                            {isProcessing ? (
                              <Loader2 className="h-4 w-4 animate-spin" />
                            ) : (
                              <Play className="h-4 w-4" />
                            )}
                            {isProcessing ? "Processing…" : "Extract"}
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </>
  );
}
