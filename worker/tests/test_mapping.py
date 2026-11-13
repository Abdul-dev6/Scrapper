from app import jobs as q
from app.osm.mapping import element_to_lead, normalize_name, registrable_domain

OSM_SRC = {"sourceUrl": "https://www.openstreetmap.org/node/42", "sourceType": "osm"}


def node(tags, **over):
    return {"type": "node", "id": 42, "lat": 51.5, "lon": -0.1, "tags": tags, **over}


def test_full_element():
    tags = {
        "name": "Iron Fist Boxing", "sport": "boxing",
        "addr:housenumber": "12", "addr:street": "High St", "addr:city": "London", "addr:postcode": "E1 6AN",
        "website": "www.ironfist.co.uk", "phone": "+44 20 7946 0000; +44 20 7946 0001",
        "email": "info@ironfist.co.uk", "contact:instagram": "@iron.fist",
        "contact:facebook": "https://facebook.com/ironfistboxing",
    }
    lead = element_to_lead(node(tags), now=q.now(), area_country_code="GB")
    assert lead["name"] == "Iron Fist Boxing"
    assert lead["categories"] == ["boxing_club"]
    assert lead["externalIds"] == {"osm": "node/42"}
    assert lead["address"] == {"street": "12 High St", "city": "London", "postcode": "E1 6AN", "countryCode": "GB"}
    assert lead["geo"] == {"type": "Point", "coordinates": [-0.1, 51.5]}  # [lng, lat]
    assert lead["website"] == {"url": "https://www.ironfist.co.uk", "domain": "ironfist.co.uk"}
    assert [p["raw"] for p in lead["phones"]] == ["+44 20 7946 0000", "+44 20 7946 0001"]
    email = dict(lead["emails"][0])
    checks = email.pop("checks")
    assert email == {"value": "info@ironfist.co.uk", "kind": "generic", **OSM_SRC}
    # syntax passed; the mail server is checked later by the crawler; nobody verified it by hand
    assert (checks["syntaxValid"], checks["mx"], checks["manuallyVerified"]) == (True, "unknown", False)
    assert lead["socials"]["instagram"]["url"] == "https://www.instagram.com/iron.fist"
    assert lead["socials"]["facebook"]["url"] == "https://facebook.com/ironfistboxing"
    assert lead["contactStatus"] == "unchecked"  # was "partial"; phones get their format check on save
    # every contact value says where it came from
    for value in [*lead["phones"], *lead["emails"], *lead["socials"].values()]:
        assert value["sourceUrl"] == OSM_SRC["sourceUrl"] and value["sourceType"] == "osm"
    assert lead["sources"][0]["url"] == OSM_SRC["sourceUrl"]


def test_missing_tags_stay_empty():
    lead = element_to_lead(node({"name": "Quiet Dojo", "amenity": "dojo"}), now=q.now())
    for field in ("address", "website", "phones", "emails", "socials", "whatsapp"):
        assert field not in lead
    assert lead["contactStatus"] == "none"


def test_unnamed_elements_are_skipped():
    assert element_to_lead(node({"sport": "boxing"}), now=q.now()) is None
    assert element_to_lead(node({"name": "  ", "sport": "boxing"}), now=q.now()) is None


def test_way_uses_center_point():
    way = {"type": "way", "id": 7, "center": {"lat": 1.5, "lon": 2.5}, "tags": {"name": "Big Gym"}}
    lead = element_to_lead(way, now=q.now())
    assert lead["geo"]["coordinates"] == [2.5, 1.5]
    assert lead["externalIds"]["osm"] == "way/7"


def test_social_page_in_website_tag_is_not_used():
    # not an official website, and socials may only come from contact:* tags
    lead = element_to_lead(node({"name": "X", "website": "https://www.facebook.com/xgym"}), now=q.now())
    assert "website" not in lead and "socials" not in lead


def test_bad_values_are_dropped():
    tags = {"name": "X", "email": "not-an-email", "website": "not a url", "contact:instagram": "https://evil.example/x",
            "contact:linkedin": "somehandle", "contact:whatsapp": "call me"}
    lead = element_to_lead(node(tags), now=q.now())
    for field in ("emails", "website", "socials", "whatsapp"):
        assert field not in lead


def test_named_vs_generic_email_and_dedupe():
    tags = {"name": "X", "email": "Coach.Sam@x.com", "contact:email": "coach.sam@x.com"}
    lead = element_to_lead(node(tags), now=q.now())
    assert [(e["value"], e["kind"]) for e in lead["emails"]] == [("Coach.Sam@x.com", "named")]


def test_country_code_only_from_tags_or_area():
    assert "address" not in element_to_lead(node({"name": "X"}), now=q.now(), area_country_code=None)
    tagged = element_to_lead(node({"name": "X", "addr:country": "ie"}), now=q.now(), area_country_code="GB")
    assert tagged["address"]["countryCode"] == "IE"  # the place's own tag wins


def test_helpers():
    assert normalize_name("  Café  Boxing-Club! ") == "cafe boxing club"
    assert registrable_domain("www.gym.co.uk") == "gym.co.uk"
    assert registrable_domain("shop.example.com") == "example.com"
