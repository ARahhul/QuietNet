"""Stage 1: context-aware normalisation (SPEC §4.4).

Dedicated ad/tracker companies collapse to their registrable root
(ads.doubleclick.net -> doubleclick.net). Big shared platforms keep the full
subdomain so we never block e.g. all of icloud.com.
"""

import tldextract

# Bundled Public Suffix List snapshot: deterministic and works offline.
_extract = tldextract.TLDExtract(suffix_list_urls=())

SHARED_PLATFORMS = {
    "google.com", "googleapis.com", "gstatic.com", "apple.com", "icloud.com",
    "amazon.com", "amazonaws.com", "microsoft.com", "windows.com",
    "akamaized.net", "akamai.net", "cloudfront.net", "fastly.net",
    "cloudflare.com", "lgtvsdp.com", "samsungcloud.com",
    # Extension beyond the SPEC list: TV vendors that serve firmware updates and
    # telemetry from the same root, so blocking the root would break updates.
    "lge.com", "samsung.com", "samsungqbe.com", "samsungcloudsolution.com",
    "samsungcloudsolution.net", "samsungosp.com",
    # Mixed-use first-party services: their tracker hosts (bat.bing.com, analytics.tiktok.com,
    # graph.facebook.com) must stay specific, or "Block" would take down the whole app or site.
    "bing.com", "msn.com", "live.com", "office.com", "skype.com", "linkedin.com",
    "facebook.com", "instagram.com", "whatsapp.com", "whatsapp.net", "fbcdn.net",
    "tiktok.com", "tiktokv.com", "youtube.com", "netflix.com", "primevideo.com",
    "yahoo.com", "twitter.com", "x.com", "snapchat.com", "spotify.com",
    "android.com", "roku.com", "xiaomi.com", "mi.com", "sony.com",
    "hotstar.com", "jio.com", "jiocinema.com", "adobe.com",
}


def root_domain(fqdn: str) -> str:
    fqdn = fqdn.lower().strip().strip(".")
    ext = _extract(fqdn)
    if not ext.suffix or not ext.domain:
        return fqdn
    return f"{ext.domain}.{ext.suffix}"


def normalize_domain(fqdn: str) -> str:
    fqdn = fqdn.lower().strip().strip(".")
    ext = _extract(fqdn)
    if not ext.suffix or not ext.domain:
        return fqdn  # bare host / unknown TLD: leave untouched
    root = f"{ext.domain}.{ext.suffix}"
    if root in SHARED_PLATFORMS and ext.subdomain:
        return fqdn  # keep full subdomain (metrics.icloud.com)
    return root  # collapse dedicated tracker (doubleclick.net)


def is_shared_platform(domain: str) -> bool:
    return root_domain(domain) in SHARED_PLATFORMS
