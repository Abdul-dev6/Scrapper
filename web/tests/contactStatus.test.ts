// Run with: npm test
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  applyManualVerification, computeContactStatus, contactStatusFilter, effectiveContactStatus,
} from "../src/lib/contactStatus";
import { parseLeadQuery } from "../src/lib/leads";
import { leadPatchSchema } from "../src/lib/validators";

const now = new Date("2026-11-14T10:00:00Z");
const base = {
  emails: [{ value: "Info@Gym.test", checks: { syntaxValid: true, mx: "valid" as const, manuallyVerified: false } }],
  phones: [{ e164: "+442079460000", raw: "020 7946 0000", checks: { formatValid: true, manuallyVerified: false } }],
};

test("status rules match the worker: automatic checks are never 'verified'", () => {
  assert.equal(computeContactStatus({}), "none");
  assert.equal(computeContactStatus({ emails: [{ value: "a@b.test", checks: { mx: "unknown" } }] }), "unchecked");
  assert.equal(computeContactStatus({ emails: [{ value: "a@b.test", checks: { mx: "no_mx" } }] }), "unchecked");
  assert.equal(computeContactStatus(base), "checked");
  assert.equal(computeContactStatus({ phones: [{ raw: "12", checks: { formatValid: false } }] }), "unchecked");
  assert.equal(computeContactStatus({ emails: [{ value: "a@b.test", mxValid: true }] }), "checked"); // legacy field
});

test("pre-migration values are shown correctly", () => {
  assert.equal(effectiveContactStatus({ contactStatus: "partial", emails: [{ value: "a@b.test" }] }), "unchecked");
  assert.equal(effectiveContactStatus({ contactStatus: "verified", ...base }), "checked");
  const manual = { contactStatus: "verified", emails: [{ value: "a@b.test", checks: { manuallyVerified: true } }] };
  assert.equal(effectiveContactStatus(manual), "verified");
});

test("filters work before and after the migration", () => {
  assert.deepEqual(contactStatusFilter("unchecked"), { contactStatus: { $in: ["unchecked", "partial"] } });
  const verified = contactStatusFilter("verified") as { contactStatus: string; $or: unknown[] };
  assert.equal(verified.contactStatus, "verified");
  assert.equal(verified.$or.length, 2); // requires an actual manual mark
  const parsed = parseLeadQuery(new URLSearchParams("contactStatus=partial")); // old bookmark
  assert.ok(parsed.success && parsed.data.contactStatus === "unchecked");
});

test("manual verification marks exactly one contact and recomputes the status", () => {
  const r = applyManualVerification(base, { kind: "email", value: "info@gym.TEST", verified: true }, "user-1", now)!;
  assert.equal(r.emails[0].checks?.manuallyVerified, true);
  assert.equal(r.emails[0].checks?.manuallyVerifiedBy, "user-1");
  assert.equal(r.phones[0].checks?.manuallyVerified, false);
  assert.equal(r.contactStatus, "verified");

  const phone = applyManualVerification(base, { kind: "phone", value: "+44 20 7946 0000", verified: true }, "u", now)!;
  assert.equal(phone.phones[0].checks?.manuallyVerified, true);

  const undo = applyManualVerification({ ...base, emails: r.emails }, { kind: "email", value: "info@gym.test", verified: false }, "u", now)!;
  assert.equal(undo.emails[0].checks?.manuallyVerified, false);
  assert.equal(undo.emails[0].checks?.manuallyVerifiedBy, undefined);
  assert.equal(undo.contactStatus, "checked");

  assert.equal(applyManualVerification(base, { kind: "email", value: "other@gym.test", verified: true }, "u", now), null);
});

test("PATCH input is strict: only the allowed fields, never owner or raw contact arrays", () => {
  assert.ok(leadPatchSchema.safeParse({ verifyContact: { kind: "email", value: "a@b.test", verified: true } }).success);
  assert.ok(!leadPatchSchema.safeParse({ ownerId: "x" }).success);
  assert.ok(!leadPatchSchema.safeParse({ emails: [] }).success);
  assert.ok(!leadPatchSchema.safeParse({ verifyContact: { kind: "email", value: "a@b.test", verified: true, by: "x" } }).success);
  assert.ok(!leadPatchSchema.safeParse({ verifyContact: { kind: "fax", value: "123", verified: true } }).success);
});
