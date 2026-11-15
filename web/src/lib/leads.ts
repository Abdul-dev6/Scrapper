import { Types, type FilterQuery } from "mongoose";
import { z } from "zod";
import { CATEGORY_IDS, CATEGORIES } from "./categories";
import {
  contactStatusFilter, effectiveContactStatus, emailMx, phoneFormatValid, type EmailChecks, type PhoneChecks,
} from "./contactStatus";

export const PAGE_SIZE = 50;
export const MAX_EXPORT_ROWS = 50_000;

const yes = z.enum(["1", "0"]).optional();
// Every filter comes from the URL, so every one is validated: unknown values are rejected,
// free text is length-limited and regex-escaped before it reaches MongoDB.
export const leadQuerySchema = z.object({
  q: z.string().trim().max(100).optional(),
  country: z.string().trim().regex(/^[A-Za-z]{2}$/).transform((s) => s.toUpperCase()).optional(),
  city: z.string().trim().max(100).optional(),
  category: z.enum(CATEGORY_IDS).optional(),
  hasEmail: yes,
  hasPhone: yes,
  hasWebsite: yes,
  // "partial" = old bookmarked URLs (before the status rename); treated as "unchecked".
  contactStatus: z.enum(["none", "unchecked", "checked", "verified", "partial"])
    .transform((s) => (s === "partial" ? "unchecked" : s))
    .optional(),
  status: z.enum(["new", "contacted", "not_interested"]).optional(),
  saved: yes,
  job: z.string().regex(/^[a-f\d]{24}$/i).optional(),
  // Data lane (licensing mode M2): all sources, OpenStreetMap only, or Overture Maps only.
  lane: z.enum(["all", "osm", "overture"]).default("all"),
  sort: z.enum(["newest", "oldest", "name", "confidence"]).default("newest"),
  page: z.coerce.number().int().min(1).max(10_000).default(1),
});
export type LeadQuery = z.infer<typeof leadQuerySchema>;

/** URLSearchParams -> validated query ("" values are treated as "not set"). */
export function parseLeadQuery(params: URLSearchParams) {
  const raw: Record<string, string> = {};
  params.forEach((v, k) => {
    if (v !== "") raw[k] = v;
  });
  return leadQuerySchema.safeParse(raw);
}

const escapeRegex = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

export function leadFilter(ownerId: Types.ObjectId, q: LeadQuery): FilterQuery<unknown> {
  const f: Record<string, unknown> = { ownerId }; // ALWAYS scoped to the signed-in user
  const and: Record<string, unknown>[] = [];
  if (q.q) {
    const rx = new RegExp(escapeRegex(q.q), "i");
    and.push({ $or: [{ name: rx }, { "address.city": rx }, { "website.domain": rx }] });
  }
  if (q.country) f["address.countryCode"] = q.country;
  if (q.city) f["address.city"] = new RegExp(`^${escapeRegex(q.city)}$`, "i");
  if (q.category) f.categories = q.category;
  if (q.hasEmail) f["emails.0"] = { $exists: q.hasEmail === "1" };
  if (q.hasPhone) {
    and.push(
      q.hasPhone === "1"
        ? { $or: [{ "phones.0": { $exists: true } }, { "whatsapp.0": { $exists: true } }] }
        : { "phones.0": { $exists: false }, "whatsapp.0": { $exists: false } }
    );
  }
  if (q.hasWebsite) f["website.url"] = { $exists: q.hasWebsite === "1" };
  if (q.contactStatus) and.push(contactStatusFilter(q.contactStatus));
  if (q.status) f.status = q.status;
  if (q.saved) f.saved = q.saved === "1" ? true : { $ne: true };
  if (q.job) f.jobIds = new Types.ObjectId(q.job);
  // Leads saved before Phase 2c have no dataLane: they are OpenStreetMap leads.
  if (q.lane === "overture") f.dataLane = "overture";
  if (q.lane === "osm") f.dataLane = { $ne: "overture" };
  if (and.length) f.$and = and;
  return f;
}

export const SORTS: Record<LeadQuery["sort"], Record<string, 1 | -1>> = {
  newest: { discoveredAt: -1, _id: -1 },
  oldest: { discoveredAt: 1, _id: 1 },
  name: { nameNormalized: 1, _id: 1 },
  confidence: { confidence: -1, _id: -1 },
};

/** Fields the table needs (the detail panel loads the full lead). */
export const LIST_FIELDS =
  "name categories address.city address.countryCode website.url website.domain website.reachable " +
  "emails.value emails.mxValid emails.checks phones.e164 phones.raw phones.checks whatsapp.e164 socials contactStatus confidence " +
  "saved status discoveredAt dataLane";

// ---------- export ----------

type Src = { sourceUrl?: string | null };
export type ExportLead = {
  _id: unknown;
  name: string;
  categories?: string[] | null;
  address?: { full?: string | null; street?: string | null; city?: string | null; region?: string | null; postcode?: string | null; country?: string | null; countryCode?: string | null } | null;
  website?: { url?: string | null; domain?: string | null; reachable?: boolean | null } | null;
  emails?: ({ value?: string | null; kind?: string | null; mxValid?: boolean | null; checks?: EmailChecks | null } & Src)[] | null;
  phones?: ({ e164?: string | null; raw?: string | null; checks?: PhoneChecks | null } & Src)[] | null;
  whatsapp?: ({ e164?: string | null; url?: string | null } & Src)[] | null;
  socials?: Record<string, ({ url?: string | null } & Src) | null | undefined> | null;
  externalIds?: { osm?: string | null; overture?: string | null } | null; // nothing from Google is ever exported
  dataLane?: string | null;
  overture?: { release?: string | null; sources?: { dataset?: string | null; license?: string | null }[] | null } | null;
  fieldSources?: Record<string, { sourceType?: string | null } | null | undefined> | null;
  verifiedAt?: Date | null;
  contactStatus?: string | null;
  confidence?: number | null;
  status?: string | null;
  saved?: boolean | null;
  notes?: string | null;
  discoveredAt?: Date | null;
};

const SOCIALS = ["instagram", "facebook", "linkedin", "youtube", "tiktok"] as const;
const catLabel = (id: string) => CATEGORIES.find((c) => c.id === id)?.label ?? id;
const join = (xs: (string | null | undefined)[]) => [...new Set(xs.filter(Boolean))].join("; ");

// Only independently sourced data (OpenStreetMap + the business's own website). Nothing from Google is
// exported, not even the place ID (it may stay in MongoDB for internal reference only).
// Contact check columns list one value per email/phone, in the same order as the Emails/Phones column.
export const EXPORT_COLUMNS = [
  "Name", "Business types", "Full address", "Street", "City", "Region", "Postcode", "Country",
  "Website", "Website answered",
  "Emails", "Email syntax", "Email MX check", "Email manually verified",
  "Phones", "Phone format", "Phone manually verified", "WhatsApp",
  ...SOCIALS.map((s) => s[0].toUpperCase() + s.slice(1)),
  "Contact status", "Confidence", "Lead status", "Saved", "Notes",
  "Data sources", "Last contact check", "OpenStreetMap", "Contact source pages", "Found on",
  "Data lane", "License", "Overture ID", "Overture release",
] as const;

const CONTACT_STATUS_TEXT: Record<string, string> = {
  none: "no contact", unchecked: "unchecked", checked: "checked automatically", verified: "verified by you",
};
const MX_TEXT = { valid: "mail server found", no_mx: "no mail server", unknown: "not checked" } as const;
const yesNo = (v: boolean | null | undefined) => (v ? "yes" : "no");

const SOURCE_NAMES: Record<string, string> = { osm: "OpenStreetMap", website: "Business website", overture: "Overture Maps" };
export const laneOf = (l: { dataLane?: string | null }) => (l.dataLane === "overture" ? "overture" : "osm");
const LANE_NAME = { osm: "OpenStreetMap", overture: "Overture Maps" } as const;

/** The licence that applies to this row's map data (per source for Overture). */
function licenseOf(l: ExportLead): string {
  if (laneOf(l) === "osm") return "ODbL 1.0 (© OpenStreetMap contributors)";
  const per = [...new Set((l.overture?.sources ?? []).map((s) => s.license).filter(Boolean))] as string[];
  return `${per.length ? per.join(" / ") : "CDLA-Permissive-2.0 / Apache-2.0"} (Overture Maps Foundation)`;
}
const day = (d?: Date | string | null) => (d ? new Date(d).toISOString().slice(0, 10) : "");
/** Like join, but keeps repeats: one status per value, in the same order as the values column. */
const each = (xs: string[]) => xs.join("; ");

function fullAddress(a: NonNullable<ExportLead["address"]>): string {
  if (a.full) return a.full;
  return [a.street, a.city, a.region, a.postcode, a.country ?? a.countryCode].filter(Boolean).join(", ");
}

function dataSources(l: ExportLead): string {
  const types = new Set<string>();
  if (l.externalIds?.osm) types.add("osm");
  for (const v of Object.values(l.fieldSources ?? {})) if (v?.sourceType) types.add(v.sourceType);
  for (const c of [...(l.emails ?? []), ...(l.phones ?? []), ...(l.whatsapp ?? [])]) {
    const t = (c as { sourceType?: string | null }).sourceType;
    if (t) types.add(t);
  }
  return [...types].map((t) => SOURCE_NAMES[t]).filter(Boolean).join("; ");
}

/** One row of plain strings (values only ever come from the stored lead, never invented). */
export function exportRow(l: ExportLead): string[] {
  const a = l.address ?? {};
  const emails = (l.emails ?? []).filter((e) => e.value);
  const phones = (l.phones ?? []).filter((p) => p.e164 || p.raw);
  const sources = [
    ...emails.map((e) => e.sourceUrl),
    ...phones.map((p) => p.sourceUrl),
    ...(l.whatsapp ?? []).map((w) => w.sourceUrl),
    ...SOCIALS.map((s) => l.socials?.[s]?.sourceUrl),
  ];
  return [
    l.name,
    (l.categories ?? []).map(catLabel).join(", "),
    fullAddress(a),
    a.street || a.full || "",
    a.city ?? "",
    a.region ?? "",
    a.postcode ?? "",
    a.countryCode ?? a.country ?? "",
    l.website?.url ?? "",
    l.website?.reachable == null ? "" : l.website.reachable ? "yes" : "no",
    each(emails.map((e) => e.value!)),
    // Stored emails always passed the syntax check (they are filtered before saving).
    each(emails.map((e) => (e.checks?.syntaxValid === false ? "invalid" : "valid"))),
    each(emails.map((e) => MX_TEXT[emailMx(e)])),
    each(emails.map((e) => yesNo(e.checks?.manuallyVerified))),
    each(phones.map((p) => (p.e164 || p.raw)!)),
    each(phones.map((p) => (phoneFormatValid(p) ? "valid number" : "not a valid number"))),
    each(phones.map((p) => yesNo(p.checks?.manuallyVerified))),
    join((l.whatsapp ?? []).map((w) => w.e164 || w.url)),
    ...SOCIALS.map((s) => l.socials?.[s]?.url ?? ""),
    CONTACT_STATUS_TEXT[effectiveContactStatus(l)] ?? "",
    l.confidence == null ? "" : String(l.confidence),
    (l.status ?? "new").replace("_", " "),
    l.saved ? "yes" : "",
    l.notes ?? "",
    dataSources(l),
    day(l.verifiedAt),
    laneOf(l) === "osm" && l.externalIds?.osm ? `https://www.openstreetmap.org/${l.externalIds.osm}` : "",
    join(sources),
    day(l.discoveredAt),
    LANE_NAME[laneOf(l)],
    licenseOf(l),
    laneOf(l) === "overture" ? l.externalIds?.overture ?? "" : "",
    laneOf(l) === "overture" ? l.overture?.release ?? "" : "",
  ];
}

export const EXPORT_ATTRIBUTION = [
  ["About this data"],
  ["The two map sources are kept apart: each row's 'Data lane' says which one it came from; rows from the two sources are never matched or merged."],
  ["OpenStreetMap rows: © OpenStreetMap contributors, available under the Open Database License (ODbL): https://www.openstreetmap.org/copyright"],
  ["If you share or publish OpenStreetMap rows, keep this attribution and share derived databases under the ODbL."],
  ["Overture Maps rows: Overture Maps Foundation, overturemaps.org. Licensed per upstream source (see each row's 'License': CDLA-Permissive-2.0 or Apache-2.0); Foursquare-sourced records: Copyright 2024 Foursquare Labs, Inc., Apache License 2.0."],
  ["Contact details were read from each business's own public website; the page is listed in 'Contact source pages'."],
  ["Emails were checked by syntax and DNS (MX) only; nobody was emailed. Phones were checked with libphonenumber."],
  ["'Email MX check' only means the email's domain accepts mail; nobody was emailed. 'Verified by you' is set only by a person."],
  ["No Google Maps data (names, addresses, place IDs or other content) is included in this file."],
];

/** The .xlsx file: a Leads sheet (plain strings, never formulas) and an attribution sheet. */
export async function buildWorkbook(rows: string[][]): Promise<Uint8Array<ArrayBuffer>> {
  const ExcelJS = (await import("exceljs")).default;
  const wb = new ExcelJS.Workbook();
  wb.creator = "Lead Finder";
  const ws = wb.addWorksheet("Leads", { views: [{ state: "frozen", ySplit: 1 }] });
  ws.addRow([...EXPORT_COLUMNS]).font = { bold: true };
  // safeCell too, in case the file is re-saved as CSV.
  for (const r of rows) ws.addRow(r.map(safeCell));
  ws.columns.forEach((c) => (c.width = 22));
  ws.autoFilter = { from: { row: 1, column: 1 }, to: { row: 1, column: EXPORT_COLUMNS.length } };
  const about = wb.addWorksheet("About this data");
  EXPORT_ATTRIBUTION.forEach((line, i) => (about.addRow(line).font = { bold: i === 0 }));
  about.getColumn(1).width = 120;
  return new Uint8Array(await wb.xlsx.writeBuffer());
}

/**
 * CSV injection: a cell starting with = + - @ (or tab/CR) can run as a formula when the file
 * is opened in Excel. A business name like "=HYPERLINK(...)" would otherwise become a link.
 * Prefixing ' makes Excel show it as text.
 */
export function safeCell(v: string): string {
  return /^[=+\-@\t\r]/.test(v) ? `'${v}` : v;
}

export function toCsv(rows: string[][]): string {
  const cell = (v: string) => {
    const s = safeCell(v);
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  // BOM so Excel opens UTF-8 (names in Urdu, Arabic, German...) correctly; CRLF per RFC 4180.
  return "﻿" + rows.map((r) => r.map(cell).join(",")).join("\r\n") + "\r\n";
}
