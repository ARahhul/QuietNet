"""AdGuard rule construction.

All users share ONE AdGuard Home, so every rule QuietNet pushes MUST carry `$client`
(SPEC §6.1). `build_adguard_rule` refuses to produce anything else and
`assert_client_scoped` is re-checked on the whole compiled section before every push.
"""

import ipaddress
import re

CLIENT_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z0-9-]{2,63}$")

SECTION_BEGIN = "! pahra:begin (managed by QuietNet - do not edit)"
SECTION_END = "! pahra:end"

ACTIONS = {"block", "allow", "quarantine"}


class RuleSecurityError(ValueError):
    """Raised when a rule would apply to clients it should not."""


def validate_client_id(cid: str) -> str:
    cid = cid.strip()
    if not CLIENT_ID_RE.match(cid):
        raise ValueError(f"invalid client id {cid!r}: use lowercase letters, digits and '-'")
    return cid


def validate_domain(domain: str) -> str:
    d = domain.strip().lower().strip(".")
    if not DOMAIN_RE.match(d):
        raise ValueError(f"invalid domain {domain!r}")
    return d


def build_adguard_rule(domain: str, client_ids: list[str], action: str = "block") -> str:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if not client_ids:
        raise RuleSecurityError("SECURITY VIOLATION: Never push a global rule without $client!")
    d = validate_domain(domain)
    ids = sorted({validate_client_id(c) for c in client_ids})
    clients_str = "|".join(f"'{cid}'" for cid in ids)
    prefix = "@@||" if action == "allow" else "||"
    return f"{prefix}{d}^$client={clients_str}"


_CLIENT_MOD_RE = re.compile(r"\$(?:[^,]*,)*client=('[a-z0-9-]+'(?:\|'[a-z0-9-]+')*)(?:,|$)")


def rule_clients(rule: str) -> list[str]:
    m = _CLIENT_MOD_RE.search(rule)
    if not m:
        return []
    return [p.strip("'") for p in m.group(1).split("|")]


def assert_client_scoped(rule: str) -> None:
    """Every non-comment QuietNet rule must name at least one valid client."""
    if not rule or rule.startswith("!") or rule.startswith("#"):
        return
    clients = rule_clients(rule)
    if not clients:
        raise RuleSecurityError(f"SECURITY VIOLATION: rule without $client: {rule!r}")
    for c in clients:
        validate_client_id(c)


def _known_ref(ref: str) -> str:
    """A client QuietNet knows: a device's ClientID, or an address / range the operator allowed in AdGuard."""
    ref = ref.strip()
    if CLIENT_ID_RE.match(ref):
        return ref
    try:
        return str(ipaddress.ip_network(ref, strict=False)) if "/" in ref else str(ipaddress.ip_address(ref))
    except ValueError:
        raise ValueError(f"not a ClientID, IP address or range: {ref!r}") from None


def rescue_rules(known: list[str], names: list[str]) -> list[str]:
    """For lookups from anyone QuietNet does NOT know (e.g. a home whose public IP just changed): answer only
    `names` (Dynamic DNS, time, QuietNet itself) and block everything else straight away.

    These are the only rules that reach beyond a household's own devices, which is the point: they cover
    strangers. Known clients are excluded by name, so their own rules apply as always.
    """
    refs = sorted({_known_ref(k) for k in known})
    if not refs:
        raise RuleSecurityError("rescue rules need at least one known client to exclude")
    scope = "$client=" + "|".join(f"~'{r}'" for r in refs)
    return [f"||*^{scope}", *(f"@@||{validate_domain(n)}^{scope}" for n in sorted(set(names)))]


def is_rescue_catch_all(rule: str) -> bool:
    return rule.startswith("||*^$client=~'")


def compile_section(rules: list[str], rescue: list[str] | None = None) -> list[str]:
    """Deterministic, de-duplicated QuietNet-managed block of user rules (rescue rules first)."""
    body = sorted(set(rules))
    for r in body:
        assert_client_scoped(r)
    return [SECTION_BEGIN, *(rescue or []), *body, SECTION_END]


def splice_section(existing: list[str], section: list[str]) -> list[str]:
    """Replace the QuietNet section inside AdGuard's user rules, keeping the operator's own lines."""
    out: list[str] = []
    inside = False
    for line in existing:
        if line == SECTION_BEGIN:
            inside = True
            continue
        if line == SECTION_END:
            inside = False
            continue
        if not inside:
            out.append(line)
    while out and out[-1] == "":
        out.pop()
    return [*out, *section]
