"""Business categories -> OpenStreetMap tag filters.

Category ids must match web/src/lib/categories.ts. Each filter is an Overpass
tag selector; a business matches a category if ANY of its selectors match.
Used by the Overpass discovery step (Step 2).
"""

CATEGORY_OSM_FILTERS: dict[str, list[str]] = {
    "gym": ['["leisure"="fitness_centre"]', '["leisure"="sports_centre"]["sport"~"fitness"]'],
    "fitness_center": ['["leisure"="fitness_centre"]', '["leisure"="sports_centre"]["sport"~"fitness|multi"]'],
    "boxing_club": ['["sport"~"(^|;)boxing(;|$)"]'],
    "mma_academy": ['["sport"~"mixed_martial_arts|mma"]'],
    "martial_arts_school": [
        '["sport"~"martial_arts|karate|judo|taekwondo|kickboxing|muay_thai|brazilian_jiu_jitsu|jiu-jitsu|aikido|kung_fu"]',
        '["amenity"="dojo"]',
    ],
    "wrestling_club": ['["sport"~"wrestling"]'],
    "sports_store": ['["shop"="sports"]'],
}

CATEGORY_IDS = frozenset(CATEGORY_OSM_FILTERS)
