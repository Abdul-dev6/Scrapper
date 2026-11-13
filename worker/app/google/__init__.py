"""Google Places API (New): optional discovery of place IDs only. OFF unless a job asks for "google".

Compliance (Google Maps Platform terms / Places policies) and decisions of 2026-11-14:
  - Only place IDs are stored (allowed indefinitely) -> googlePlaceRefs.placeId. Never linked to a lead.
  - Names, addresses, coordinates and other Google content are NOT stored, matched or exported.
  - Nothing from Google, not even place IDs, appears in Excel/CSV exports.
  - Place Details is not called (PlacesClient.place_details exists but is unused).
"""
