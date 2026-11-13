import { Schema, model, models, type InferSchemaType, type Model } from "mongoose";
import { CATEGORY_IDS } from "@/lib/categories";

// Leads are WRITTEN by the Python worker and READ/updated (saved, status, notes) by the web app.
// Every contact value carries where it was found, so nothing in the table is unexplained.
// "google" appears only in `sources` (a Google Maps link by place ID), never on a stored value.
const SOURCE_TYPES = ["osm", "website", "search", "google"] as const;
const sourceFields = {
  sourceUrl: { type: String, required: true },
  sourceType: { type: String, enum: SOURCE_TYPES, required: true },
};

const socialSchema = new Schema(
  { url: String, reachable: Boolean, checkedAt: Date, ...sourceFields },
  { _id: false }
);

const leadSchema = new Schema(
  {
    ownerId: { type: Schema.Types.ObjectId, ref: "User", required: true },
    name: { type: String, required: true, trim: true, maxlength: 300 },
    nameNormalized: { type: String, required: true },
    categories: [{ type: String, enum: CATEGORY_IDS }],
    address: {
      full: String,
      street: String,
      city: String,
      region: String,
      postcode: String,
      country: String,
      countryCode: String,
    },
    geo: {
      type: new Schema(
        {
          type: { type: String, enum: ["Point"], required: true },
          coordinates: { type: [Number], required: true }, // [lng, lat]
        },
        { _id: false }
      ),
      default: undefined, // absent instead of {} so the 2dsphere index stays valid
    },
    website: {
      url: String,
      domain: String, // registrable domain, e.g. "examplegym.co.uk"
      reachable: Boolean,
      checkedAt: Date,
    },
    // mxValid is the legacy MX result; `checks` replaces it (see lib/contactStatus.ts). Both are kept
    // until the contact-status migration has run everywhere.
    emails: [{
      _id: false, value: String, kind: { type: String, enum: ["generic", "named"] }, mxValid: Boolean, ...sourceFields,
      checks: {
        type: new Schema({
          syntaxValid: Boolean,
          mx: { type: String, enum: ["valid", "no_mx", "unknown"] },
          checkedAt: Date,
          manuallyVerified: { type: Boolean, default: false },
          manuallyVerifiedAt: Date,
          manuallyVerifiedBy: { type: Schema.Types.ObjectId, ref: "User" },
        }, { _id: false }),
        default: undefined,
      },
    }],
    phones: [{
      _id: false, e164: String, raw: String, ...sourceFields,
      checks: {
        type: new Schema({
          formatValid: Boolean,
          checkedAt: Date,
          manuallyVerified: { type: Boolean, default: false },
          manuallyVerifiedAt: Date,
          manuallyVerifiedBy: { type: Schema.Types.ObjectId, ref: "User" },
        }, { _id: false }),
        default: undefined,
      },
    }],
    whatsapp: [{ _id: false, e164: String, url: String, ...sourceFields }],
    socials: {
      instagram: { type: socialSchema, default: undefined },
      facebook: { type: socialSchema, default: undefined },
      linkedin: { type: socialSchema, default: undefined },
      youtube: { type: socialSchema, default: undefined },
      tiktok: { type: socialSchema, default: undefined },
    },
    // googlePlaceId: Google allows storing place IDs indefinitely; no other Google content is stored.
    externalIds: { osm: String, osmAliases: { type: [String], default: undefined }, foursquare: String, googlePlaceId: String },
    // Provenance of non-contact fields: { name: { sourceType, sourceUrl }, address: ..., geo: ..., website: ... }
    fieldSources: { type: Schema.Types.Mixed, default: undefined },
    // name + ~110 m grid cell; backs a unique index so two workers can't insert the same business at once
    dedupeKey: String,
    googleMatch: { matchedAt: Date, method: String },
    googleSeenAt: Date,
    mergeLog: { type: [{ _id: false, at: Date, osm: String, reason: String }], default: undefined },
    sources: [{ _id: false, url: String, type: { type: String, enum: SOURCE_TYPES }, fetchedAt: Date }],
    // "verified" = a person verified a contact. "partial" is the pre-migration name of "unchecked".
    contactStatus: { type: String, enum: ["none", "unchecked", "checked", "verified", "partial"], default: "none" },
    contactsVersion: Number, // 2 = every email/phone has `checks` (set by the worker or the migration)
    confidence: { type: Number, min: 0, max: 1, default: 0 },
    saved: { type: Boolean, default: false },
    status: { type: String, enum: ["new", "contacted", "not_interested"], default: "new" },
    notes: { type: String, maxlength: 5000, default: "" },
    jobIds: [{ type: Schema.Types.ObjectId, ref: "ScrapingJob" }],
    discoveredAt: { type: Date, required: true },
    verifiedAt: Date,
  },
  { timestamps: true, collection: "leads" }
);

// Duplicate protection: one lead per OSM place. Partial (not sparse) because ownerId is always
// present in the compound key.
// website.domain is deliberately NOT unique: branches of a chain share one website but are
// separate businesses (separate addresses/phones). Domain-level merging happens in Step 3.
leadSchema.index({ ownerId: 1, "website.domain": 1 });
leadSchema.index(
  { ownerId: 1, "externalIds.osm": 1 },
  { unique: true, partialFilterExpression: { "externalIds.osm": { $type: "string" } } }
);
// Same business under another OSM id (node + building) is merged into one lead: its id becomes an alias.
leadSchema.index({ ownerId: 1, "externalIds.osmAliases": 1 });
// One lead per Google place, per user.
leadSchema.index(
  { ownerId: 1, "externalIds.googlePlaceId": 1 },
  { unique: true, partialFilterExpression: { "externalIds.googlePlaceId": { $type: "string" } } }
);
leadSchema.index({ ownerId: 1, dedupeKey: 1 }, { unique: true, partialFilterExpression: { dedupeKey: { $type: "string" } } });
// Duplicate lookups by the worker (same phone / same name under another id)
leadSchema.index({ ownerId: 1, "phones.e164": 1 });
leadSchema.index({ ownerId: 1, nameNormalized: 1 });
// Filters used by the leads table
leadSchema.index({ ownerId: 1, "address.countryCode": 1, "address.city": 1 });
leadSchema.index({ ownerId: 1, categories: 1 });
leadSchema.index({ ownerId: 1, saved: 1, updatedAt: -1 });
leadSchema.index({ ownerId: 1, contactStatus: 1 });
leadSchema.index({ name: "text", "address.city": "text" });
leadSchema.index({ geo: "2dsphere" });

export type LeadDoc = InferSchemaType<typeof leadSchema>;
export const Lead: Model<LeadDoc> = models.Lead || model<LeadDoc>("Lead", leadSchema);
