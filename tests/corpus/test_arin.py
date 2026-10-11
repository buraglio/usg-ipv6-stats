import responses as resp
import pytest
from usg_ipv6.corpus.arin import fetch_asns_for_org, search_orgs_by_name


@resp.activate
def test_fetch_asns_for_org_returns_list():
    resp.add(
        resp.GET,
        "https://whois.arin.net/rest/org/USGSA/asns.json",
        json={"asns": {"asn": ["396260", "5722"]}},
        status=200,
    )
    result = fetch_asns_for_org("USGSA")
    assert result == [396260, 5722]


@resp.activate
def test_rate_limit_triggers_retry():
    resp.add(resp.GET, "https://whois.arin.net/rest/org/USGSA/asns.json", status=429)
    resp.add(
        resp.GET,
        "https://whois.arin.net/rest/org/USGSA/asns.json",
        json={"asns": {"asn": ["396260"]}},
        status=200,
    )
    result = fetch_asns_for_org("USGSA")
    assert result == [396260]
    assert len(resp.calls) == 2


@resp.activate
def test_fetch_asns_returns_empty_on_404():
    resp.add(resp.GET, "https://whois.arin.net/rest/org/DOESNOTEXIST/asns.json", status=404)
    result = fetch_asns_for_org("DOESNOTEXIST")
    assert result == []
