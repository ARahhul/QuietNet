import pytest

from pahra.pipeline.normalize import normalize_domain, root_domain


@pytest.mark.parametrize(
    "fqdn,expected",
    [
        ("ads.doubleclick.net", "doubleclick.net"),
        ("googleads.g.doubleclick.net", "doubleclick.net"),
        ("tkacr.alphonso.tv", "alphonso.tv"),
        ("metrics.icloud.com", "metrics.icloud.com"),
        ("icloud.com", "icloud.com"),
        ("device-metrics-us.amazon.com", "device-metrics-us.amazon.com"),
        ("us.lgtvsdp.com", "us.lgtvsdp.com"),
        ("news.bbc.co.uk", "bbc.co.uk"),
        ("Ads.DoubleClick.NET.", "doubleclick.net"),
        ("localhost", "localhost"),
        # mixed-use services keep the tracker host, so blocking it doesn't take down the app
        ("bat.bing.com", "bat.bing.com"),
        ("analytics.tiktok.com", "analytics.tiktok.com"),
        ("graph.facebook.com", "graph.facebook.com"),
        ("youtube.com", "youtube.com"),
    ],
)
def test_normalize(fqdn, expected):
    assert normalize_domain(fqdn) == expected


def test_root_domain_uses_public_suffix_list():
    assert root_domain("bbc.co.uk") == "bbc.co.uk"
    assert root_domain("a.b.bbc.co.uk") == "bbc.co.uk"
