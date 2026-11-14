// Place-name normalization. MUST match worker/app/geo/text.py `norm` exactly: the gazetteer's search
// keys are built by the worker, and the web app normalizes what the user types the same way.
// Both test suites check the same examples (web/tests/placeText.test.ts, worker/tests/test_geo.py).
export function norm(text: string | null | undefined): string {
  return (text ?? "")
    .toLowerCase()
    .normalize("NFKD")
    .replace(/\p{M}/gu, "") // all marks: accents, and spacing marks such as Devanagari vowel signs
    .replace(/[^\p{L}\p{N}]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Gazetteer ids: "gn:2643743" (city), "admin1:GB.ENG" (region), "country:GB". */
export const PLACE_ID_RE = /^(gn:\d{1,12}|admin1:[A-Z]{2}\.[A-Za-z0-9]{1,20}|country:[A-Z]{2})$/;

/** "London, UK" -> { name: "london", qualifiers: ["uk"] } */
export function parsePlaceQuery(q: string): { name: string; qualifiers: string[] } {
  const [first = "", ...rest] = q.split(",");
  return { name: norm(first), qualifiers: rest.map(norm).filter(Boolean) };
}
