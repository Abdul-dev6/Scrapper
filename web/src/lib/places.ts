import { Gazetteer, GazetteerMeta } from "@/models";
import { PLACE_ID_RE, parsePlaceQuery } from "./placeText";

// Offline place search over the gazetteer (GeoNames). Replaces public Nominatim (decision 2026-11-14):
// nothing the user types is sent to an outside service.
export const PLACE_ATTRIBUTION = { text: "Place names: GeoNames (CC BY 4.0)", url: "https://www.geonames.org/" };

export type PlaceCandidate = {
  id: string;
  kind: "city" | "admin1" | "country";
  name: string;
  label: string; // "London, England, United Kingdom"
  detail: string; // "City · population 8,961,989"
  countryCode: string;
  population: number;
};

type Doc = {
  _id: string; kind: "city" | "admin1" | "country"; name: string; countryCode?: string | null; countryName?: string | null;
  admin1Name?: string | null; population?: number | null;
};

const KIND_LABEL = { city: "City or town", admin1: "Region", country: "Country" } as const;
const escapeRegex = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function toCandidate(d: Doc): PlaceCandidate {
  const parts =
    d.kind === "city" ? [d.name, d.admin1Name, d.countryName] : d.kind === "admin1" ? [d.name, d.countryName] : [d.name];
  const pop = d.population ?? 0;
  return {
    id: d._id,
    kind: d.kind,
    name: d.name,
    label: parts.filter(Boolean).join(", "),
    detail: `${KIND_LABEL[d.kind]}${pop ? ` · population ${pop.toLocaleString("en-GB")}` : ""}`,
    countryCode: d.countryCode ?? "",
    population: pop,
  };
}

/** Candidates for what the user typed, biggest first. More than one = the user must choose. */
export async function searchPlaces(q: string, limit = 8): Promise<PlaceCandidate[]> {
  const { name, qualifiers } = parsePlaceQuery(q);
  if (name.length < 2) return [];
  const ctx = qualifiers.length ? { ctxKeys: { $all: qualifiers } } : {};
  const fields = "kind name countryCode countryName admin1Name population";
  let docs = await Gazetteer.find({ keys: name, ...ctx }).sort({ population: -1 }).limit(limit).select(fields).lean();
  if (!docs.length) {
    // Nothing named exactly that: names that START with it ("Manch" -> Manchester). Index-assisted.
    docs = await Gazetteer.find({ keys: { $regex: `^${escapeRegex(name)}` }, ...ctx })
      .sort({ population: -1 })
      .limit(limit)
      .select(fields)
      .lean();
  }
  return (docs as unknown as Doc[]).map(toCandidate);
}

/** The full gazetteer document for a picked place, as stored on the job (the worker builds the area from it). */
export async function placeForJob(id: string): Promise<Record<string, unknown> | null> {
  if (!PLACE_ID_RE.test(id)) return null;
  const doc = await Gazetteer.findById(id).select("-keys -ctxKeys").lean();
  if (!doc) return null;
  const meta = await GazetteerMeta.findById("gazetteer").select("files loadedAt").lean();
  const sha = (meta?.files as Record<string, { sha256?: string }> | undefined)?.["cities1000.zip"]?.sha256;
  return { ...doc, gazetteerVersion: sha ? `geonames#${sha.slice(0, 12)}` : "geonames" };
}
