import { Schema, model, models, type InferSchemaType, type Model } from "mongoose";

// Place search data (GeoNames, CC BY 4.0), WRITTEN only by the worker's loader
// (`python -m app.geo.gazetteer load ...`, atomic swap), READ by the web app's place search.
// Shared across users on purpose: public place data, no user data. See worker/app/geo/gazetteer.py.
const gazetteerSchema = new Schema(
  {
    _id: { type: String, required: true }, // "gn:2643743" | "admin1:GB.ENG" | "country:GB"
    kind: { type: String, enum: ["city", "admin1", "country"], required: true },
    name: { type: String, required: true },
    countryCode: String,
    countryName: String,
    admin1Code: String,
    admin1Name: String,
    lat: Number,
    lon: Number,
    population: Number,
    geonameId: Number,
    featureCode: String,
    cityBbox: { type: [Number], default: undefined }, // admin1 only: [south, west, north, east] of its towns
    keys: [String], // normalized names of the place itself
    ctxKeys: [String], // normalized names/codes of its country and region
  },
  { collection: "gazetteer", versionKey: false }
);
// Same names as the loader creates them, so `npm run db:indexes` keeps (not rebuilds) them.
gazetteerSchema.index({ keys: 1, population: -1 }, { name: "keys_1_population_-1" });
gazetteerSchema.index({ kind: 1, countryCode: 1 }, { name: "kind_1_countryCode_1" });

const gazetteerMetaSchema = new Schema(
  {
    _id: { type: String, required: true },
    source: String,
    license: String,
    files: Schema.Types.Mixed, // { "cities1000.zip": { sha256, bytes }, ... }
    counts: Schema.Types.Mixed,
    loadedAt: Date,
  },
  { collection: "gazetteerMeta", versionKey: false }
);

export type GazetteerDoc = InferSchemaType<typeof gazetteerSchema>;
export const Gazetteer: Model<GazetteerDoc> = models.Gazetteer || model<GazetteerDoc>("Gazetteer", gazetteerSchema);
export type GazetteerMetaDoc = InferSchemaType<typeof gazetteerMetaSchema>;
export const GazetteerMeta: Model<GazetteerMetaDoc> =
  models.GazetteerMeta || model<GazetteerMetaDoc>("GazetteerMeta", gazetteerMetaSchema);
