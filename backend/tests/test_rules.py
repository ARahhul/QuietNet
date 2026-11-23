import random
import string

import pytest

from pahra.adguard.rules import (
    SECTION_BEGIN,
    SECTION_END,
    RuleSecurityError,
    assert_client_scoped,
    build_adguard_rule,
    compile_section,
    optout_rule,
    rule_clients,
    splice_section,
)


def test_never_builds_rule_without_client():
    with pytest.raises(RuleSecurityError):
        build_adguard_rule("alphonso.tv", [])


def test_rule_shapes_match_spec():
    # `important`: a home's own rules outrank the shared lists, and a home that switched the shared list off
    assert build_adguard_rule("alphonso.tv", ["rahhul-tv"]) == "||alphonso.tv^$client='rahhul-tv',important"
    assert (
        build_adguard_rule("alphonso.tv", ["rahhul-tv", "rahhul-phone", "rahhul-laptop"])
        == "||alphonso.tv^$client='rahhul-laptop'|'rahhul-phone'|'rahhul-tv',important"
    )
    assert build_adguard_rule("lgtvsdp.com", ["rahhul-tv"], "allow") == "@@||lgtvsdp.com^$client='rahhul-tv',important"
    assert build_adguard_rule("x.com", ["a"], "quarantine") == "||x.com^$client='a',important"
    assert rule_clients("||x.com^$client='a'|'b',important") == ["a", "b"]


def test_optout_rule_lets_everything_through_for_those_devices_only():
    assert optout_rule(["tv-x", "phone-y", "tv-x"]) == "@@||*^$client='phone-y'|'tv-x'"
    assert_client_scoped(optout_rule(["a"]))
    with pytest.raises(RuleSecurityError):
        optout_rule([])
    with pytest.raises(ValueError):
        optout_rule(["a'|'*"])


@pytest.mark.parametrize("bad", ["Rahhul TV", "a'|'*", "x,important", "", "-x", "a" * 64])
def test_rejects_bad_client_ids(bad):
    with pytest.raises(ValueError):
        build_adguard_rule("alphonso.tv", [bad])


@pytest.mark.parametrize("bad", ["alphonso.tv^$important", "*.com", "a b.com", "com", "x.com|y.com"])
def test_rejects_bad_domains(bad):
    with pytest.raises(ValueError):
        build_adguard_rule(bad, ["a"])


def test_assert_client_scoped_catches_global_rules():
    for global_rule in ["||alphonso.tv^", "@@||lgtvsdp.com^", "||x.com^$important", "||x.com^$client="]:
        with pytest.raises(RuleSecurityError):
            assert_client_scoped(global_rule)
    assert_client_scoped("! a comment")


def test_compiled_section_is_always_client_scoped_property():
    rng = random.Random(7)
    rules = []
    for _ in range(500):
        n = rng.randint(1, 4)
        ids = ["".join(rng.choices(string.ascii_lowercase, k=rng.randint(1, 10))) for _ in range(n)]
        dom = "".join(rng.choices(string.ascii_lowercase, k=8)) + rng.choice([".com", ".tv", ".co.uk"])
        rules.append(build_adguard_rule(dom, ids, rng.choice(["block", "allow", "quarantine"])))
    section = compile_section(rules)
    assert section[0] == SECTION_BEGIN and section[-1] == SECTION_END
    for line in section[1:-1]:
        assert rule_clients(line), line


def test_compile_refuses_injected_global_rule():
    with pytest.raises(RuleSecurityError):
        compile_section([build_adguard_rule("a.com", ["x"]), "||evil.com^"])


def test_splice_keeps_operator_rules_and_replaces_section():
    existing = ["||operator-own.com^", SECTION_BEGIN, "||old.com^$client='a'", SECTION_END]
    new = compile_section([build_adguard_rule("new.com", ["a"])])
    out = splice_section(existing, new)
    assert out == ["||operator-own.com^", SECTION_BEGIN, "||new.com^$client='a',important", SECTION_END]
    assert splice_section(out, new) == out
