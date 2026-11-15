// Phase 2c: OSM and Overture are separate data lanes in filters and exports. Run with: npm test
import assert from "node:assert/strict";
import { test } from "node:test";
import { Types } from "mongoose";
import { EXPORT_ATTRIBUTION, EXPORT_COLUMNS, exportRow, leadFilter, parseLeadQuery, type ExportLead } from "../src/lib/leads";

const owner = new Types.ObjectId();
const col = (row: string[], name: (typeof EXPORT_COLUMNS)[number]) => row[EXPORT_COLUMNS.indexOf(name)];
const q = (s: string) => {
  const p = parseLeadQuery(new URLSearchParams(s));
  assert.ok(p.success);
  return p.data;
};

test("source filter: all / OSM only (incl. leads from before Phase 2c) / Overture only", () => {
  assert.equal(leadFilter(owner, q("")).dataLane, undefined);
  assert.deepEqual(leadFilter(owner, q("lane=osm")).dataLane, { $ne: "overture" }); // missing dataLane = OSM
  assert.equal(leadFilter(owner, q("lane=overture")).dataLane, "overture");
  assert.equal(leadFilter(owner, q("lane=overture")).ownerId, owner); // still scoped to the user
  assert.ok(!parseLeadQuery(new URLSearchParams("lane=google")).success);
});

const osmLead: ExportLead = { _id: 1, name: "Iron Fist", externalIds: { osm: "node/1" } };
const ovLead: ExportLead = {
  _id: 2, name: "Iron Fist Gym", dataLane: "overture",
  externalIds: { overture: "gers-iron", googlePlaceId: "ChIJsecret" } as ExportLead["externalIds"],
  overture: { release: "2026-10-21.0", sources: [{ dataset: "meta", license: "CDLA-Permissive-2.0" }, { dataset: "Foursquare", license: "Apache-2.0" }] },
};

test("export rows say which lane and licence they come from", () => {
  const o = exportRow(osmLead);
  assert.equal(col(o, "Data lane"), "OpenStreetMap");
  assert.match(col(o, "License"), /ODbL/);
  assert.equal(col(o, "OpenStreetMap"), "https://www.openstreetmap.org/node/1");
  assert.equal(col(o, "Overture ID"), "");
  const v = exportRow(ovLead);
  assert.equal(col(v, "Data lane"), "Overture Maps");
  assert.equal(col(v, "License"), "CDLA-Permissive-2.0 / Apache-2.0 (Overture Maps Foundation)");
  assert.equal(col(v, "Overture ID"), "gers-iron");
  assert.equal(col(v, "Overture release"), "2026-10-21.0");
  assert.equal(col(v, "OpenStreetMap"), "");
  assert.ok(!v.some((c) => c.includes("ChIJsecret"))); // still nothing from Google
});

test("attribution names both sources separately", () => {
  const text = EXPORT_ATTRIBUTION.flat().join("\n");
  assert.match(text, /OpenStreetMap rows: © OpenStreetMap contributors.*ODbL/);
  assert.match(text, /Overture Maps rows: Overture Maps Foundation, overturemaps\.org/);
  assert.match(text, /never matched or merged/);
});
