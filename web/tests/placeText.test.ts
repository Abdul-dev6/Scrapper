// Run with: npm test
import assert from "node:assert/strict";
import { test } from "node:test";
import { PLACE_ID_RE, norm, parsePlaceQuery } from "../src/lib/placeText";

// Same examples as worker/tests/test_geo.py NORM_EXAMPLES: both sides must normalize identically.
const NORM_EXAMPLES: [string, string][] = [
  ["London", "london"], ["São Paulo", "sao paulo"], ["Köln", "koln"], ["U.K.", "u k"],
  ["  New   York  ", "new york"], ["Saint-Étienne", "saint etienne"], ["İstanbul", "istanbul"],
  ["Straße", "straße"], ["O'Fallon", "o fallon"], ["snake_case", "snake case"], ["لاہور", "لاہور"],
  ["東京", "東京"], ["नई दिल्ली", "नई दलल"], ["", ""],
];

test("normalization matches the worker", () => {
  for (const [raw, expected] of NORM_EXAMPLES) assert.equal(norm(raw), expected, raw);
});

test("query parsing: first part is the place, the rest narrow it down", () => {
  assert.deepEqual(parsePlaceQuery("London, UK"), { name: "london", qualifiers: ["uk"] });
  assert.deepEqual(parsePlaceQuery(" London , Ontario , Canada "), { name: "london", qualifiers: ["ontario", "canada"] });
  assert.deepEqual(parsePlaceQuery("Germany,,"), { name: "germany", qualifiers: [] });
});

test("place ids are strictly validated", () => {
  for (const ok of ["gn:2643743", "admin1:GB.ENG", "admin1:CA.08", "country:GB"]) assert.ok(PLACE_ID_RE.test(ok), ok);
  for (const bad of ["gn:", "gn:12a", "country:gb", "admin1:GB", "admin1:GB.$where", "x:1", "gn:1; drop"]) {
    assert.ok(!PLACE_ID_RE.test(bad), bad);
  }
});
