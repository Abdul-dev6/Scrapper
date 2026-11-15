// Keep these ids in sync with worker/app/categories.py (the worker owns the OSM tag mapping).
export const CATEGORIES = [
  { id: "gym", label: "Gyms" },
  { id: "boxing_club", label: "Boxing clubs" },
  { id: "mma_academy", label: "MMA academies" },
  { id: "martial_arts_school", label: "Martial arts schools" },
  { id: "wrestling_club", label: "Wrestling clubs" },
  { id: "fitness_center", label: "Fitness centers" },
  { id: "sports_store", label: "Sports equipment stores" },
] as const;

export type CategoryId = (typeof CATEGORIES)[number]["id"];
export const CATEGORY_IDS = CATEGORIES.map((c) => c.id) as [CategoryId, ...CategoryId[]];

// "google" = Google Places (New) for discovery/corroboration only; leads keep only OSM/website data.
// "overture" = Overture Maps Places: a separate data lane, never matched with OpenStreetMap (M2).
export const SOURCES = ["osm", "overture", "google"] as const;
export type SourceId = (typeof SOURCES)[number];
