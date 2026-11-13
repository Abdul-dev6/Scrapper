// Run with: npm test
import assert from "node:assert/strict";
import { test } from "node:test";
import { CONTACTS_VERSION, migrateLeadContacts } from "../src/lib/contactMigration";

const verifiedAt = new Date("2026-11-01T00:00:00Z");

test("old 'verified' (automatic) becomes 'checked'; mxValid maps to checks.mx", () => {
  const { set, from, to } = migrateLeadContacts({
    contactStatus: "verified",
    verifiedAt,
    emails: [{ value: "a@gym.test", mxValid: true }, { value: "b@gym.test", mxValid: false }, { value: "c@gym.test" }],
    phones: [{ e164: "+442079460000", raw: "020 7946 0000" }, { raw: "12" }],
  });
  assert.equal(from, "verified");
  assert.equal(to, "checked");
  const emails = set.emails as { mxValid?: boolean; checks: Record<string, unknown> }[];
  assert.deepEqual(emails.map((e) => e.checks.mx), ["valid", "no_mx", "unknown"]);
  assert.ok(emails.every((e) => e.checks.syntaxValid === true && e.checks.manuallyVerified === false));
  assert.equal(emails[0].mxValid, true); // legacy field kept during the switchover
  assert.deepEqual(emails[0].checks.checkedAt, verifiedAt);
  const phones = set.phones as { checks: Record<string, unknown> }[];
  assert.deepEqual(phones.map((p) => p.checks.formatValid), [true, false]);
  assert.equal(set.contactsVersion, CONTACTS_VERSION);
});

test("old 'partial' becomes 'unchecked'; no contacts stays 'none'", () => {
  assert.equal(migrateLeadContacts({ contactStatus: "partial", emails: [{ value: "a@gym.test" }] }).to, "unchecked");
  assert.equal(migrateLeadContacts({ contactStatus: "none" }).to, "none");
});

test("migration is idempotent and keeps checks/manual marks written by the new code", () => {
  const lead = {
    contactStatus: "verified",
    emails: [{ value: "a@gym.test", checks: { syntaxValid: true, mx: "no_mx" as const, manuallyVerified: true } }],
  };
  const once = migrateLeadContacts(lead);
  assert.equal(once.to, "verified"); // a real manual verification survives
  const twice = migrateLeadContacts({ ...lead, ...(once.set as object) });
  assert.deepEqual(twice.set, once.set);
});
