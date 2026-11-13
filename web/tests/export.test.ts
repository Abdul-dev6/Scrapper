// Run with: npm test
import assert from "node:assert/strict";
import { test } from "node:test";
import ExcelJS from "exceljs";
import { EXPORT_COLUMNS, buildWorkbook, exportRow, toCsv, type ExportLead } from "../src/lib/leads";
import { monthKey } from "../src/lib/googleUsage";

const lead: ExportLead = {
  _id: "x",
  name: "Iron Fist",
  categories: ["boxing_club"],
  address: { street: "12 High St", city: "London", postcode: "N1 1AA", countryCode: "GB" },
  website: { url: "https://ironfist.test", domain: "ironfist.test", reachable: true },
  emails: [
    { value: "info@ironfist.test", mxValid: true, sourceUrl: "https://ironfist.test/contact", sourceType: "website" } as never,
    { value: "old@ironfist.test", mxValid: false, sourceUrl: "https://www.openstreetmap.org/node/1", sourceType: "osm" } as never,
  ],
  phones: [
    { e164: "+442079460001", raw: "020 7946 0001", sourceUrl: "https://ironfist.test/contact" },
    { raw: "12345", sourceUrl: "https://www.openstreetmap.org/node/1" },
  ],
  // A legacy lead may still hold a Google place ID in MongoDB; it must never reach an export.
  externalIds: { osm: "node/1", googlePlaceId: "ChIJironfist001" } as ExportLead["externalIds"],
  fieldSources: { name: { sourceType: "osm" }, address: { sourceType: "osm" } },
  verifiedAt: new Date("2026-11-10T10:00:00Z"),
  discoveredAt: new Date("2026-11-01T10:00:00Z"),
};

const col = (row: string[], name: (typeof EXPORT_COLUMNS)[number]) => row[EXPORT_COLUMNS.indexOf(name)];

test("export row has the required lead fields with verification status and provenance", () => {
  const row = exportRow(lead);
  assert.equal(row.length, EXPORT_COLUMNS.length);
  assert.equal(col(row, "Name"), "Iron Fist");
  assert.equal(col(row, "City"), "London");
  assert.equal(col(row, "Country"), "GB");
  assert.equal(col(row, "Full address"), "12 High St, London, N1 1AA, GB");
  assert.equal(col(row, "Website"), "https://ironfist.test");
  assert.equal(col(row, "Emails"), "info@ironfist.test; old@ironfist.test");
  // one status per email/phone, same order as the values; legacy mxValid still understood
  assert.equal(col(row, "Email syntax"), "valid; valid");
  assert.equal(col(row, "Email MX check"), "mail server found; no mail server");
  assert.equal(col(row, "Email manually verified"), "no; no");
  assert.equal(col(row, "Phones"), "+442079460001; 12345");
  assert.equal(col(row, "Phone format"), "valid number; not a valid number");
  assert.equal(col(row, "Phone manually verified"), "no; no");
  assert.equal(col(row, "Data sources"), "OpenStreetMap; Business website");
  assert.equal(col(row, "Last contact check"), "2026-11-10");
});

test("nothing from Google is ever exported, not even the place ID", async () => {
  assert.ok(!EXPORT_COLUMNS.some((c) => /google/i.test(c)));
  const row = exportRow(lead);
  assert.ok(!row.some((cell) => cell.includes("ChIJironfist001")));
  const csv = toCsv([[...EXPORT_COLUMNS], row]);
  assert.ok(!csv.includes("ChIJironfist001"));
});

test("machine checks are labelled 'checked automatically', manual ones 'verified by you'", () => {
  const legacyVerified = { ...lead, contactStatus: "verified" }; // pre-migration meaning: automatic checks passed
  assert.equal(col(exportRow(legacyVerified), "Contact status"), "checked automatically");
  const manual = {
    ...lead,
    contactStatus: "verified",
    emails: [{ value: "info@ironfist.test", checks: { syntaxValid: true, mx: "valid", manuallyVerified: true } } as never],
  };
  const row = exportRow(manual);
  assert.equal(col(row, "Contact status"), "verified by you");
  assert.equal(col(row, "Email manually verified"), "yes");
});

test("xlsx has the leads sheet, an attribution sheet and no formulas", async () => {
  const evil = { ...lead, name: "=HYPERLINK(\"http://evil\")" };
  const bytes = await buildWorkbook([exportRow(lead), exportRow(evil)]);
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(Buffer.from(bytes) as never);
  const sheet = wb.getWorksheet("Leads")!;
  assert.deepEqual((sheet.getRow(1).values as string[]).slice(1), [...EXPORT_COLUMNS]);
  assert.equal(sheet.rowCount, 3);
  const name = sheet.getRow(3).getCell(1);
  assert.equal(name.type, ExcelJS.ValueType.String); // text, not a formula
  assert.equal(name.value, "'=HYPERLINK(\"http://evil\")");
  const about = wb.getWorksheet("About this data")!;
  const text = JSON.stringify(about.getSheetValues());
  assert.match(text, /OpenStreetMap contributors/);
  assert.match(text, /ODbL/);
});

test("csv escapes formulas and keeps UTF-8 BOM", () => {
  const csv = toCsv([["a", "=1+1", "x,y"]]);
  assert.ok(csv.startsWith("﻿"));
  assert.ok(csv.includes("'=1+1") && csv.includes('"x,y"'));
});

test("Google usage month follows Pacific time like the worker", () => {
  assert.equal(monthKey(new Date("2026-11-01T06:00:00Z"), "America/Los_Angeles"), "2026-10");
  assert.equal(monthKey(new Date("2026-11-01T08:00:00Z"), "America/Los_Angeles"), "2026-11");
});
