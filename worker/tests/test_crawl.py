import asyncio

import httpx
import pytest

from app.crawl.extract import extract, social_profile, whatsapp_number
from app.crawl.fetcher import Fetcher, FetchError
from app.crawl.robots import Robots
from app.crawl.runner import BOT_TOKEN, make_async_client
from app.crawl.site import crawl_site, link_score
from app.crawl.verify import MxChecker, clean_email, email_kind, phone_e164
from tests.conftest import FakeWeb

UA = "LeadFinderBot/0.2 (+https://lead-finder.test/crawler)"

HOME = """<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "SportsActivityLocation", "name": "Iron Fist",
 "telephone": "+44 20 7946 0000", "email": "mailto:info@ironfist.test",
 "sameAs": ["https://www.instagram.com/ironfistboxing/", "https://www.facebook.com/sharer/sharer.php?u=x"]}
</script></head><body>
<nav><a href="/classes">Classes</a><a href="/about-us">About us</a></nav>
<a href="https://www.facebook.com/ironfistgym">Facebook</a>
<a href="https://twitter.com/intent/tweet?url=x">Tweet</a>
<a href="https://other-site.test/contact">Partner contact</a>
<footer><a href="/contact">Contact</a><a href="/privacy.pdf">Privacy</a></footer>
</body></html>"""

CONTACT = """<html><body>
<p>Email the coach: coach.mike@ironfist.test or call <a href="tel:020%207946%200001">020 7946 0001</a></p>
<a href="https://wa.me/447911123456">WhatsApp us</a>
<a href="mailto:bookings@ironfist.test?subject=Hi">Book</a>
<img src="logo@2x.png"> sentry@sentry.wixpress.com
<a href="https://www.linkedin.com/company/iron-fist-boxing">LinkedIn</a>
</body></html>"""


def run(coro):
    return asyncio.run(coro)


async def _crawl(web: FakeWeb, url: str, max_pages=5, **fetcher_kw):
    async with make_async_client(UA, 5, web.transport()) as client:
        fetcher = Fetcher(client, Robots(BOT_TOKEN), delay=0, **fetcher_kw)
        return await crawl_site(fetcher, url, max_pages=max_pages)


# ---------- extraction ----------

def test_extract_reads_jsonld_links_and_text():
    found = extract(HOME, "https://ironfist.test/")
    assert found.used_jsonld
    assert found.emails == ["info@ironfist.test"]
    assert found.phones == ["+44 20 7946 0000"]
    assert found.socials == {
        "instagram": "https://www.instagram.com/ironfistboxing",
        "facebook": "https://www.facebook.com/ironfistgym",  # the "share" link was ignored
    }
    footer = [(u, t) for u, t, in_footer in found.links if in_footer]
    assert ("https://ironfist.test/contact", "Contact") in footer

    found = extract(CONTACT, "https://ironfist.test/contact")
    assert "coach.mike@ironfist.test" in found.emails and "bookings@ironfist.test" in found.emails
    assert found.phones == ["020 7946 0001"]
    assert found.whatsapp == ["+447911123456"]
    assert found.socials["linkedin"] == "https://www.linkedin.com/company/iron-fist-boxing"


def test_broken_jsonld_does_not_break_the_page():
    html = '<script type="application/ld+json">{oops</script><a href="mailto:hi@gym.test">mail</a>'
    assert extract(html, "https://gym.test/").emails == ["hi@gym.test"]


def test_emails_inside_scripts_are_ignored():
    html = "<script>var x = 'tracker@analytics.test';</script><p>no email here</p>"
    assert extract(html, "https://gym.test/").emails == []


@pytest.mark.parametrize("url, expected", [
    ("https://www.instagram.com/mygym/", ("instagram", "https://www.instagram.com/mygym")),
    ("https://instagram.com/p/Cxyz123/", None),  # a post, not the profile
    ("https://www.facebook.com/profile.php?id=1234", ("facebook", "https://www.facebook.com/profile.php?id=1234")),
    ("https://www.facebook.com/sharer.php?u=x", None),
    ("https://www.linkedin.com/in/someone", ("linkedin", "https://www.linkedin.com/in/someone")),
    ("https://www.linkedin.com/shareArticle?url=x", None),
    ("https://www.tiktok.com/@mygym", ("tiktok", "https://www.tiktok.com/@mygym")),
    ("https://www.youtube.com/watch?v=abc", None),
    ("https://www.youtube.com/@mygym", ("youtube", "https://www.youtube.com/@mygym")),
    ("https://mygym.test/instagram", None),
])
def test_social_profile(url, expected):
    assert social_profile(url) == expected


def test_whatsapp_links():
    assert whatsapp_number("https://wa.me/447911123456") == "+447911123456"
    assert whatsapp_number("https://api.whatsapp.com/send?phone=92%20300%201234567") == "+923001234567"
    assert whatsapp_number("https://wa.me/") is None


# ---------- verification ----------

def test_clean_email_rejects_fake_ones():
    assert clean_email("Info@IronFist.TEST.") == "Info@ironfist.test"
    assert clean_email("logo@2x.png") is None
    assert clean_email("you@example.com") is None
    assert clean_email("605a7baede844d278b89dc95ae0a9123@sentry-next.wixpress.com") is None
    assert clean_email("not an email") is None
    assert clean_email("dataprotection@gym.test") is None and clean_email("no-reply@gym.test") is None
    assert clean_email("privacy.team@gym.test") is None and clean_email("hristo@gym.test") == "hristo@gym.test"
    assert email_kind("info@gym.test") == "generic" and email_kind("mike@gym.test") == "named"


def test_phone_e164_uses_the_country():
    assert phone_e164("020 7946 0001", "GB") == "+442079460001"
    assert phone_e164("+44 20 7946 0000", None) == "+442079460000"
    assert phone_e164("0300 1234567", "PK") == "+923001234567"
    assert phone_e164("12345", "GB") is None
    assert phone_e164("020 7946 0001", None) is None  # no country and no +: can't know, so don't guess


def test_mx_is_looked_up_once_per_domain():
    web = FakeWeb(no_mx={"dead.test"})
    mx = MxChecker(web.resolve)

    async def go():
        return [await mx.check(e) for e in ("a@gym.test", "b@gym.test", "c@dead.test")]

    assert run(go()) == [True, True, False]
    assert web.mx_lookups == ["gym.test", "dead.test"]


# ---------- polite fetching ----------

def test_site_crawl_follows_contact_links_on_its_own_domain_only():
    web = FakeWeb(pages={"https://ironfist.test/": HOME, "https://ironfist.test/contact": CONTACT})
    result = run(_crawl(web, "https://ironfist.test/"))
    assert result.reachable
    assert result.pages == ["https://ironfist.test/", "https://ironfist.test/contact"]
    emails = {v.value: v.source_url for v in result.emails}
    assert emails["info@ironfist.test"] == "https://ironfist.test/"
    assert emails["coach.mike@ironfist.test"] == "https://ironfist.test/contact"
    visited = web.visited()
    assert not any("other-site.test" in u or u.endswith(".pdf") for u in visited)
    assert "https://ironfist.test/classes" not in visited  # not a contact-type page


def test_max_pages_is_respected():
    links = "".join(f'<a href="/contact-{i}">Contact {i}</a>' for i in range(10))
    web = FakeWeb(pages={"https://gym.test/": f"<html>{links}</html>"})
    run(_crawl(web, "https://gym.test/", max_pages=5))
    assert len(web.visited()) == 5


def test_robots_txt_disallow_is_obeyed():
    web = FakeWeb(pages={"https://gym.test/": HOME}, robots={"gym.test": "User-agent: *\nDisallow: /"})
    result = run(_crawl(web, "https://gym.test/"))
    assert not result.reachable and "robots" in result.error
    assert web.visited() == []


def test_robots_txt_for_our_bot_only():
    robots = "User-agent: LeadFinderBot\nDisallow: /contact\n\nUser-agent: *\nAllow: /"
    web = FakeWeb(pages={"https://ironfist.test/": HOME, "https://ironfist.test/contact": CONTACT},
                  robots={"ironfist.test": robots})
    result = run(_crawl(web, "https://ironfist.test/"))
    assert result.reachable and "https://ironfist.test/contact" not in web.visited()


def test_robots_server_error_means_stay_away():
    web = FakeWeb(pages={"https://gym.test/": HOME}, status={"https://gym.test/robots.txt": 503})
    web.robots = {}

    original = web.handler

    def handler(request):
        if request.url.path == "/robots.txt":
            web.calls.append(request)
            return httpx.Response(503)
        return original(request)

    web.handler = handler
    result = run(_crawl(web, "https://gym.test/"))
    assert not result.reachable and web.visited() == []


def test_same_site_requests_are_spaced_out():
    web = FakeWeb(pages={"https://ironfist.test/": HOME, "https://ironfist.test/contact": CONTACT})
    clock = [100.0]
    waits = []

    async def fake_sleep(s):
        waits.append(s)
        clock[0] += s

    async def go():
        async with make_async_client(UA, 5, web.transport()) as client:
            fetcher = Fetcher(client, Robots(BOT_TOKEN), delay=2.5, sleep=fake_sleep, clock=lambda: clock[0])
            await crawl_site(fetcher, "https://ironfist.test/")

    run(go())
    assert waits == [2.5]  # 2 pages -> one pause of 2.5 s between them


def test_robots_crawl_delay_is_honoured():
    web = FakeWeb(pages={"https://ironfist.test/": HOME, "https://ironfist.test/contact": CONTACT},
                  robots={"ironfist.test": "User-agent: *\nCrawl-delay: 10"})
    clock = [0.0]
    waits = []

    async def fake_sleep(s):
        waits.append(s)
        clock[0] += s

    async def go():
        async with make_async_client(UA, 5, web.transport()) as client:
            fetcher = Fetcher(client, Robots(BOT_TOKEN), delay=2.5, sleep=fake_sleep, clock=lambda: clock[0])
            await crawl_site(fetcher, "https://ironfist.test/")

    run(go())
    assert waits == [10]


def test_non_html_and_errors_are_skipped():
    pdf = httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
    web = FakeWeb(pages={"https://gym.test/": pdf})
    result = run(_crawl(web, "https://gym.test/"))
    assert not result.reachable and "HTML" in result.error
    result = run(_crawl(FakeWeb(), "https://nothing.test/"))
    assert not result.reachable and result.error == "HTTP 404"


def test_honest_user_agent_is_sent():
    web = FakeWeb(pages={"https://gym.test/": "<html></html>"})
    run(_crawl(web, "https://gym.test/"))
    assert all(r.headers["user-agent"] == UA for r in web.calls)


def test_link_score():
    assert link_score("https://g.test/kontakt", "", False) == 3
    assert link_score("https://g.test/x", "Contact us", True) == 4
    assert link_score("https://g.test/impressum", "", False) == 2
    assert link_score("https://g.test/shop", "Shop", False) == 0


def test_fetch_rejects_non_web_urls():
    async def go():
        async with make_async_client(UA, 5, FakeWeb().transport()) as client:
            await Fetcher(client, Robots(BOT_TOKEN), delay=0).fetch("ftp://gym.test/")

    with pytest.raises(FetchError):
        run(go())


def test_fine_print_pages_are_not_followed():
    html = ('<a href="/contact-us/complaints-procedure">Complaints</a><a href="/legal/membership-agreement">Terms</a>'
            '<a href="/privacy-policy">Privacy</a><a href="/impressum">Impressum</a>')
    web = FakeWeb(pages={"https://gym.test/": html})
    run(_crawl(web, "https://gym.test/"))
    assert web.visited() == ["https://gym.test/", "https://gym.test/impressum"]


def test_email_scan_is_linear_on_huge_pages_and_finds_the_same_addresses():
    """The old unbounded pattern was quadratic on long letter runs: a 2 MB page (the fetch cap) took
    minutes inside the crawler's event loop, stalling every other site and the job heartbeat."""
    import random
    import re
    import time

    from app.crawl.extract import emails_in_text

    old = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
    rng = random.Random(7)
    for alphabet in ("abcXYZ09._%+- @@..\n", "ab@.c", "a@.", "x@ .-"):
        for _ in range(2000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 150)))
            text += rng.choice(["", " info@gym.co.uk", " a.b+c@x-y.example.org.", " @@x.com", "mail:team@sub.dom.ain.io,"])
            assert emails_in_text(text) == old.findall(text), text
    for text, found in [("x" * 2_000_000 + " front@huge.test", ["front@huge.test"]), ("a@" * 1_000_000, []),
                        (("x" * 63 + "@") * 30_000, []), ("q@w." * 500_000, [])]:
        t0 = time.perf_counter()
        assert emails_in_text(text) == found
        assert time.perf_counter() - t0 < 10  # measured 0.001-2.8 s; the old scan did not finish in minutes
