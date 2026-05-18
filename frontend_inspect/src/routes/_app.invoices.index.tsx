import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { FileText, Download, Search, X } from "lucide-react";
import { supabase } from "@/integrations/supabase/client";
import { useOrg } from "@/lib/org";
import { usePermissions } from "@/lib/permissions";
import { PageHeader } from "@/components/app/PageHeader";
import { StatusBadge } from "@/components/app/StatusBadge";
import { EmptyState } from "@/components/app/EmptyState";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
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

export const Route = createFileRoute("/_app/invoices/")({
  component: InvoicesList,
});

type Row = Record<string, unknown> & { id: string };

const REVIEW_STATUSES = ["pending", "in_review", "approved", "rejected", "needs_info"] as const;
const PARSE_STATUSES = ["completed", "pending", "failed"] as const;

const initialFilters = {
  search: "",
  supplierId: "all",
  reviewStatus: "all",
  parseStatus: "all",
  linked: "all", // all | linked | missing
  fromDate: "",
  toDate: "",
  minAmount: "",
  maxAmount: "",
};

function InvoicesList() {
  const { currentOrgId } = useOrg();
  const { canEditData } = usePermissions();
  const navigate = useNavigate();
  const [filters, setFilters] = useState(initialFilters);

  const setF = <K extends keyof typeof filters>(k: K, v: (typeof filters)[K]) =>
    setFilters((p) => ({ ...p, [k]: v }));

  const { data, isLoading, error } = useQuery({
    queryKey: ["invoices_extracted", currentOrgId],
    queryFn: async () => {
      let q = supabase
        .from("invoices_extracted")
        .select("*, supplier:suppliers(id, supplier_name, name)")
        .order("invoice_date", { ascending: false, nullsFirst: false })
        .limit(500);
      if (currentOrgId) q = q.eq("organisation_id", currentOrgId);
      const primary = await q;
      if (!primary.error) return (primary.data ?? []) as Row[];

      let q2 = supabase.from("invoices_extracted").select("*").limit(500);
      if (currentOrgId) q2 = q2.eq("organisation_id", currentOrgId);
      const inv = await q2;
      if (inv.error) throw inv.error;
      const rows = (inv.data ?? []) as Row[];
      const ids = Array.from(
        new Set(
          rows.map((r) => r.supplier_id as string | undefined).filter((v): v is string => Boolean(v)),
        ),
      );
      const supplierMap = new Map<string, Record<string, unknown>>();
      if (ids.length > 0) {
        const sup = await supabase.from("suppliers").select("*").in("id", ids);
        for (const s of (sup.data ?? []) as Record<string, unknown>[]) {
          supplierMap.set(s.id as string, s);
        }
      }
      return rows.map((r) => ({
        ...r,
        supplier: r.supplier_id ? (supplierMap.get(r.supplier_id as string) ?? null) : null,
      })) as Row[];
    },
  });

  const supplierOptions = useMemo(() => {
    const map = new Map<string, string>();
    for (const r of data ?? []) {
      const sid = r.supplier_id as string | null | undefined;
      if (!sid) continue;
      const sup = r.supplier as Record<string, unknown> | null | undefined;
      const name =
        pickStr(sup ?? {}, ["supplier_name", "name"]) ||
        pickStr(r, ["supplier_name_extracted", "supplier_name"]) ||
        sid;
      if (!map.has(sid)) map.set(sid, name);
    }
    return Array.from(map.entries())
      .map(([id, name]) => ({ id, name }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [data]);

  const filtered = useMemo(() => {
    if (!data) return [];
    const s = filters.search.trim().toLowerCase();
    const from = filters.fromDate ? new Date(filters.fromDate).getTime() : null;
    const to = filters.toDate ? new Date(filters.toDate).getTime() + 86_399_999 : null;
    const min = filters.minAmount ? Number(filters.minAmount) : null;
    const max = filters.maxAmount ? Number(filters.maxAmount) : null;

    return data.filter((r) => {
      if (s) {
        const ref = (pickStr(r, ["invoice_number", "reference", "number"]) || "").toLowerCase();
        const fname = (pickStr(r, ["file_name", "filename", "source_file"]) || "").toLowerCase();
        if (!ref.includes(s) && !fname.includes(s)) return false;
      }
      if (filters.supplierId !== "all" && (r.supplier_id as string | null) !== filters.supplierId)
        return false;
      if (filters.reviewStatus !== "all" && r.review_status !== filters.reviewStatus) return false;
      if (filters.parseStatus !== "all" && r.parse_status !== filters.parseStatus) return false;
      if (filters.linked === "linked" && !r.supplier_id) return false;
      if (filters.linked === "missing" && r.supplier_id) return false;

      const dateStr = pickStr(r, ["invoice_date", "issue_date", "date"]);
      const ts = dateStr ? new Date(dateStr).getTime() : null;
      if (from != null && (ts == null || ts < from)) return false;
      if (to != null && (ts == null || ts > to)) return false;

      const total = pickNum(r, ["total_amount", "total", "amount", "grand_total"]);
      if (min != null && (total == null || total < min)) return false;
      if (max != null && (total == null || total > max)) return false;

      return true;
    });
  }, [data, filters]);

  const hasActiveFilters = JSON.stringify(filters) !== JSON.stringify(initialFilters);

  return (
    <>
      <PageHeader
        title="Invoices"
        description="Parsed invoices captured by APPayPal."
        actions={
          <>
            {canEditData && (
              <Button asChild variant="outline" size="sm" className="gap-2">
                <Link to="/invoices/upload">
                  <FileText className="h-4 w-4" /> Upload
                </Link>
              </Button>
            )}
            <Button variant="outline" size="sm" className="gap-2">
              <Download className="h-4 w-4" /> Export
            </Button>
          </>
        }
      />
      <Card className="card-elevated overflow-hidden">
        <div className="border-b bg-surface-muted/50 px-4 py-3 space-y-3">
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">Search</Label>
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={filters.search}
                  onChange={(e) => setF("search", e.target.value)}
                  placeholder="Invoice # or file…"
                  className="h-9 w-56 pl-8"
                />
              </div>
            </div>

            <FilterField label="Supplier">
              <Select value={filters.supplierId} onValueChange={(v) => setF("supplierId", v)}>
                <SelectTrigger className="h-9 w-44"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All suppliers</SelectItem>
                  {supplierOptions.map((s) => (
                    <SelectItem key={s.id} value={s.id}>{s.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FilterField>

            <FilterField label="Review status">
              <Select value={filters.reviewStatus} onValueChange={(v) => setF("reviewStatus", v)}>
                <SelectTrigger className="h-9 w-36"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All</SelectItem>
                  {REVIEW_STATUSES.map((s) => (
                    <SelectItem key={s} value={s}>{s.replace("_", " ")}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FilterField>

            <FilterField label="Parse status">
              <Select value={filters.parseStatus} onValueChange={(v) => setF("parseStatus", v)}>
                <SelectTrigger className="h-9 w-36"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All</SelectItem>
                  {PARSE_STATUSES.map((s) => (
                    <SelectItem key={s} value={s}>{s}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FilterField>

            <FilterField label="Supplier linked">
              <Select value={filters.linked} onValueChange={(v) => setF("linked", v)}>
                <SelectTrigger className="h-9 w-40"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All</SelectItem>
                  <SelectItem value="linked">Linked supplier</SelectItem>
                  <SelectItem value="missing">Missing supplier</SelectItem>
                </SelectContent>
              </Select>
            </FilterField>

            <FilterField label="From">
              <Input type="date" value={filters.fromDate} onChange={(e) => setF("fromDate", e.target.value)} className="h-9 w-36" />
            </FilterField>
            <FilterField label="To">
              <Input type="date" value={filters.toDate} onChange={(e) => setF("toDate", e.target.value)} className="h-9 w-36" />
            </FilterField>

            <FilterField label="Min amount">
              <Input type="number" inputMode="decimal" value={filters.minAmount} onChange={(e) => setF("minAmount", e.target.value)} className="h-9 w-28" placeholder="0" />
            </FilterField>
            <FilterField label="Max amount">
              <Input type="number" inputMode="decimal" value={filters.maxAmount} onChange={(e) => setF("maxAmount", e.target.value)} className="h-9 w-28" placeholder="∞" />
            </FilterField>

            {hasActiveFilters && (
              <Button variant="ghost" size="sm" className="gap-1 self-end" onClick={() => setFilters(initialFilters)}>
                <X className="h-3.5 w-3.5" /> Clear
              </Button>
            )}
          </div>
          <div className="text-xs text-muted-foreground">
            {filtered.length.toLocaleString()} {filtered.length === 1 ? "result" : "results"}
            {data ? ` of ${data.length}` : ""}
          </div>
        </div>

        <CardContent className="p-0">
          {isLoading ? (
            <div className="p-8 text-sm text-muted-foreground">Loading invoices…</div>
          ) : error ? (
            <div className="p-8 text-sm text-destructive">{(error as Error).message}</div>
          ) : !filtered || filtered.length === 0 ? (
            <EmptyState
              icon={FileText}
              title={hasActiveFilters ? "No matches" : "No invoices yet"}
              description={
                hasActiveFilters
                  ? "Try adjusting your filters."
                  : "Invoices captured for the current organisation will appear here."
              }
            />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Reference</TableHead>
                  <TableHead>Supplier</TableHead>
                  <TableHead>Date</TableHead>
                  <TableHead className="text-right">Total</TableHead>
                  <TableHead>Status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filtered.map((r) => {
                  const ref =
                    pickStr(r, ["invoice_number", "reference", "number"]) ||
                    `#${String(r.id).slice(0, 8)}`;
                  const sup = r.supplier as Record<string, unknown> | null | undefined;
                  const supplier =
                    pickStr(sup ?? {}, ["supplier_name", "name"]) ||
                    pickStr(r, ["supplier_name_extracted", "supplier_name", "supplier", "vendor"]) ||
                    "—";
                  const date = pickStr(r, ["invoice_date", "issue_date", "date"]);
                  const total = pickNum(r, ["total_amount", "total", "amount", "grand_total"]);
                  const status =
                    pickStr(r, ["review_status", "parse_status", "status", "state"]) || "captured";
                  const currency = pickStr(r, ["currency", "currency_code"]) || "USD";
                  return (
                    <TableRow
                      key={r.id}
                      className="cursor-pointer"
                      onClick={() => {
                        void navigate({
                          to: "/invoices/$invoiceId",
                          params: { invoiceId: String(r.id) },
                        });
                      }}
                    >
                      <TableCell className="font-medium">
                        <Link
                          to="/invoices/$invoiceId"
                          params={{ invoiceId: r.id }}
                          className="text-foreground hover:text-primary"
                          onClick={(e) => e.stopPropagation()}
                        >
                          {ref}
                        </Link>
                      </TableCell>
                      <TableCell className="text-foreground/90">{supplier}</TableCell>
                      <TableCell className="text-muted-foreground">
                        {date ? new Date(date).toLocaleDateString() : "—"}
                      </TableCell>
                      <TableCell className="text-right font-medium tabular-nums">
                        {total != null
                          ? total.toLocaleString(undefined, { style: "currency", currency })
                          : "—"}
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={status} />
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

function FilterField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      {children}
    </div>
  );
}

function pickStr(r: Record<string, unknown>, keys: string[]): string | null {
  for (const k of keys) {
    const v = r[k];
    if (typeof v === "string" && v.length > 0) return v;
    if (typeof v === "number") return String(v);
  }
  return null;
}
function pickNum(r: Record<string, unknown>, keys: string[]): number | null {
  for (const k of keys) {
    const v = r[k];
    if (typeof v === "number") return v;
    if (typeof v === "string" && v && !Number.isNaN(Number(v))) return Number(v);
  }
  return null;
}
