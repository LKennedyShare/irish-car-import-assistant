"""External buyer integration tests: mocked public HTTP only, never real payments."""

import builtins
import base64
import copy
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from irish_car_import_assistant import assistant
from irish_car_import_assistant import payments as payment


CAR = dict(make="Volkswagen", model="Golf", year=2023, mileage_km=50000,
           fuel_type="Petrol", co2_g_km=125)
FLAGS = ["--make", "Volkswagen", "--model", "Golf", "--year", "2023",
         "--mileage-km", "50000", "--fuel-type", "Petrol", "--co2-g-km", "125"]


@pytest.fixture(autouse=True)
def forbid_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected real network or shared payment call")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(payment, "run", forbidden)
    monkeypatch.setattr(payment.Account, "from_key", forbidden)
    monkeypatch.setattr(payment, "x402ClientSync", forbidden)


@pytest.fixture
def payload():
    demo = assistant.compare_car(demo=True)
    return {key: copy.deepcopy(demo[key])
            for key in ("irish_market", "verdict", "routes", "assumptions", "estimate_only")}


def respond(monkeypatch, *, payload=None, status=200, headers=None, content=None, inspect=None):
    original = httpx.Client
    calls = []

    def handler(request):
        calls.append(request)
        if inspect:
            inspect(request)
        if content is not None:
            return httpx.Response(status, content=content, headers=headers)
        return httpx.Response(status, content=json.dumps(payload), headers=headers)

    def client(**kwargs):
        # An actual Client with an in-memory transport preserves redirect/header semantics.
        instance = original(transport=httpx.MockTransport(handler), **kwargs)
        instance.send = REAL_SEND.__get__(instance, original)
        return instance

    monkeypatch.setattr(assistant.httpx, "Client", client)
    return calls


REAL_SEND = httpx.Client.send
REAL_PAYMENT_RUN = payment.run
REAL_SDK = payment.x402ClientSync


def test_demo_never_imports_helper_or_sdk_or_reads_wallet(monkeypatch):
    original = builtins.__import__
    monkeypatch.setenv("X402_CLIENT_PRIVATE_KEY", "do-not-use")

    def guarded(name, *args, **kwargs):
        if name.startswith(("eth_account", "x402")) or name == "irish_car_import_assistant":
            pytest.fail(f"Offline demo imported payment dependency: {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    result = assistant.compare_car(demo=True)
    assert result["mode"] == "SYNTHETIC_OFFLINE_DEMO"
    assert result["vehicle"] == {**CAR, "origin": "GB"}
    costs = result["routes"][0]["landed_cost"]
    assert sum(costs[key] for key in assistant.COSTS) == pytest.approx(38010.60)
    assert result["verdict"]["saving_eur"] == -8060.60
    assert result["settlement"] is None
    assert "synthetic" in result["assumptions"][0].lower()
    assert "listings" not in result["routes"][0]


@pytest.mark.parametrize("wallet", [False, True])
def test_free_exact_query_attribution_and_no_upgrade(monkeypatch, payload, wallet):
    if wallet:
        monkeypatch.setenv("X402_CLIENT_PRIVATE_KEY", "never-read-or-sign")
    calls = respond(monkeypatch, payload=payload)
    result = assistant.compare_car(**CAR, transmission="manual", origin="gb")
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "GET"
    assert request.url.path == "/api/v1/a2a/uk-import-preview"
    assert dict(request.url.params) == {
        **{key: str(value) for key, value in CAR.items()}, "transmission": "Manual", "origin": "GB",
    }
    assert request.headers["X-Client-Name"] == "car-import-assistant"
    assert "car-import-assistant" in request.headers["User-Agent"]
    assert "payment-signature" not in request.headers
    assert result["mode"] == "LIVE_FREE_PREVIEW"
    assert result["payment_status"] == "not_authorized"
    assert "reserved" in result["listings_note"]


@pytest.mark.parametrize("fuel", [
    "Petrol", "Diesel", "Electric", "Petrol Hybrid", "Diesel Hybrid",
    "Petrol Plug-in Hybrid", "Diesel Plug-in Hybrid",
])
def test_supported_fuel_types_preserve_their_meaning(monkeypatch, payload, fuel):
    assert fuel in set(assistant.FUELS.values())
    calls = respond(monkeypatch, payload=payload)
    assistant.compare_car(**{**CAR, "fuel_type": fuel.lower()})
    assert calls[0].url.params["fuel_type"] == fuel


@pytest.mark.parametrize("fuel", ["Hybrid", "Plug-in Hybrid", "PHEV"])
def test_ambiguous_hybrid_fuel_is_not_guessed(fuel):
    with pytest.raises(ValueError, match="specify the hybrid"):
        assistant.compare_car(**{**CAR, "fuel_type": fuel})


@pytest.mark.parametrize("status,headers,match", [
    (404, {}, "Broaden filters"),
    (429, {"Retry-After": "3600"}, "Retry-After: 3600"),
    (503, {}, "service unavailable"),
    (402, {}, "no automatic retry or payment upgrade"),
    (302, {"Location": "https://example.org"}, "redirect refused"),
    (307, {"Location": "/same-origin"}, "redirect refused"),
])
def test_errors_never_pay_or_retry(monkeypatch, status, headers, match):
    calls = respond(monkeypatch, status=status, headers=headers, payload={"detail": "unavailable"})
    with pytest.raises(ValueError, match=match):
        assistant.compare_car(**CAR)
    assert len(calls) == 1


@pytest.mark.parametrize("change", [
    lambda p: p.pop("verdict"),
    lambda p: p.update(irish_market=[]),
    lambda p: p["irish_market"].update(median_price_eur=float("nan")),
    lambda p: p["irish_market"].update(comparable_count=True),
    lambda p: p["irish_market"].update(comparable_count=-1),
    lambda p: p["verdict"].update(recommendation="IMPORT_NOW"),
    lambda p: p["verdict"].update(recommendation=[]),
    lambda p: p["verdict"].update(best_origin="NI"),
    lambda p: p["verdict"].update(saving_eur=0),
    lambda p: p.update(routes=[]),
    lambda p: p["routes"][0].update(origin="IE"),
    lambda p: p["routes"][0].update(landed_cost={}),
    lambda p: p["routes"][0]["landed_cost"].update(shipping_eur=-1),
    lambda p: p["routes"][0]["landed_cost"].update(vrt_eur=float("inf")),
    lambda p: p["routes"][0]["landed_cost"].update(other_costs_eur=True),
    lambda p: p["routes"][0]["landed_cost"].update(total_landed_cost_eur=1),
    lambda p: p["routes"][0].update(comparable_count=1.5),
    lambda p: p["routes"][0].update(recommendation="UNKNOWN"),
    lambda p: p.update(assumptions={}),
    lambda p: p.update(estimate_only=False),
])
def test_malformed_success_rejected(monkeypatch, payload, change):
    change(payload)
    respond(monkeypatch, payload=payload)
    with pytest.raises(ValueError, match="Malformed response"):
        assistant.compare_car(**CAR)


@pytest.mark.parametrize("body", [b"not json", b"[]", b"null", b'{"irish_market":true}'])
def test_bad_json_and_shape(monkeypatch, body):
    respond(monkeypatch, content=body)
    with pytest.raises(ValueError, match="Malformed"):
        assistant.compare_car(**CAR)


@pytest.mark.parametrize("field,value", [
    ("year", 1949), ("year", 2101), ("year", True), ("year", 2023.0),
    ("mileage_km", -1), ("mileage_km", False), ("co2_g_km", -1),
    ("co2_g_km", 1001), ("co2_g_km", float("nan")), ("co2_g_km", True),
    ("make", ""), ("model", "  "), ("fuel_type", "banana"),
    ("origin", "IE"), ("transmission", "cvt"), ("pay", True),
    ("pay", "false"), ("full", 1),
])
def test_invalid_input_fails_before_network(field, value):
    with pytest.raises(ValueError):
        assistant.compare_car(**{**CAR, field: value})


@pytest.mark.parametrize("field", assistant.REQUIRED)
def test_missing_live_fields_fail_before_network(field):
    car = CAR.copy()
    car.pop(field)
    with pytest.raises(ValueError, match="Missing required live fields"):
        assistant.compare_car(**car)


@pytest.mark.parametrize("amount", ["NaN", "sNaN", "Infinity", "-Infinity", "0", "-0.01",
                                  "0.020001", True, False, float("nan"), None, "bad"])
def test_invalid_caps_rejected_even_in_demo(amount):
    with pytest.raises(ValueError, match="max-usdc"):
        assistant.compare_car(demo=True, max_usdc=amount)


def test_demo_pay_guard():
    with pytest.raises(ValueError, match="cannot be used with --demo"):
        assistant.compare_car(demo=True, full=True, pay=True)


@pytest.mark.parametrize("url", [
    "http://example.org", "https://user:password@example.org", "https://example.org/path",
    "https://example.org?query=1", "https://example.org#fragment", "https://example.org\\evil",
    "ftp://example.org", "https://example.org:bad",
])
def test_unsafe_base_urls_fail_before_network(url):
    with pytest.raises(ValueError):
        assistant.compare_car(**CAR, base_url=url)


@pytest.mark.parametrize("url", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"])
def test_loopback_http_allowed(monkeypatch, payload, url):
    calls = respond(monkeypatch, payload=payload)
    assistant.compare_car(**CAR, base_url=url)
    assert len(calls) == 1


def test_full_inspection_delegates_without_signing(monkeypatch):
    captured = []

    def run(args, *, report_errors):
        captured.append((args, report_errors))
        return {"status": 402, "selected": {"amount": "20000", "network": "eip155:8453"},
                "settlement": None}

    monkeypatch.setattr(payment, "run", run)
    result = assistant.compare_car(**CAR, full=True)
    args, report_errors = captured[0]
    assert args.pay is False
    assert args.max_usdc == Decimal("0.02")
    assert args.client_name == assistant.CLIENT_NAME
    assert report_errors is False
    assert result["mode"] == "LIVE_QUOTE_INSPECTION"
    assert result["payment_status"] == "not_authorized"
    assert result["quote"]["amount"] == "20000"
    assert "No payment made" in result["message"]
    assert "--full --pay" in result["message"]
    assert "Unpaid quote" in assistant.format_comparison(result)
    assert "verdict" not in result


def test_shared_helper_quote_http_contract(monkeypatch):
    """Exercise the real shared helper; its embedding API must preserve attribution."""
    monkeypatch.setattr(payment, "run", REAL_PAYMENT_RUN)
    monkeypatch.setattr(payment, "x402ClientSync", REAL_SDK)
    monkeypatch.setattr(
        REAL_SDK, "create_payment_payload",
        lambda *a, **kw: pytest.fail("Quote inspection must never sign"),
    )
    challenge = {
        "x402Version": 2,
        "resource": {"url": assistant.BASE_URL + "/api/v1/a2a/uk-import-check"},
        "accepts": [{
            "scheme": "exact", "network": "eip155:8453",
            "asset": payment.USDC_ASSETS["eip155:8453"],
            "amount": "20000", "payTo": "0x" + "12" * 20,
            "maxTimeoutSeconds": 60, "extra": {"name": "USD Coin", "version": "2"},
        }],
    }
    calls = respond(
        monkeypatch, status=402, payload={"error": "Payment required"},
        headers={"PAYMENT-REQUIRED": base64.b64encode(json.dumps(challenge).encode()).decode()},
    )
    result = assistant.compare_car(**CAR, full=True)
    assert result["mode"] == "LIVE_QUOTE_INSPECTION"
    assert result["payment_status"] == "not_authorized"
    assert len(calls) == 1
    assert calls[0].headers["X-Client-Name"] == assistant.CLIENT_NAME
    assert assistant.CLIENT_NAME in calls[0].headers["User-Agent"]
    assert "PAYMENT-SIGNATURE" not in calls[0].headers


def test_shared_helper_preserves_rate_limit_guidance(monkeypatch, capsys):
    monkeypatch.setattr(payment, "run", REAL_PAYMENT_RUN)
    monkeypatch.setattr(payment, "x402ClientSync", REAL_SDK)
    calls = respond(monkeypatch, status=429, headers={"Retry-After": "3600"}, payload={})
    assert assistant.main([*FLAGS, "--full", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Retry-After: 3600" in captured.err
    assert len(calls) == 1


@pytest.mark.parametrize("settlement,expected", [
    ({"success": True, "transaction": "test-only"}, "confirmed"),
    (None, "unconfirmed"), ({}, "unconfirmed"), ({"success": "true"}, "unconfirmed"),
])
def test_full_paid_one_helper_call_capped_same_query(monkeypatch, payload, settlement, expected):
    calls = []
    payload["routes"][0]["cheapest_matching_listings"] = [
        {"url": f"https://example.org/synthetic-{i}", "make": "Volkswagen", "model": "Golf",
         "year": 2023, "mileage_km": 50000, "price_eur": 23000, "currency": "GBP"}
        for i in range(5)
    ]

    def run(args, *, report_errors):
        calls.append((args, report_errors))
        return {"status": 200, "result": payload, "settlement": settlement}

    monkeypatch.setattr(payment, "run", run)
    result = assistant.compare_car(**CAR, origin="GB", full=True, pay=True, max_usdc="0.015")
    assert len(calls) == 1
    args, report_errors = calls[0]
    assert report_errors is False
    assert args.pay is True
    assert args.max_usdc == Decimal("0.015")
    assert args.base_url == assistant.BASE_URL
    assert args.client_name == "car-import-assistant"
    parsed = urlsplit(args.path)
    assert parsed.path == "/api/v1/a2a/uk-import-check"
    assert parse_qs(parsed.query) == {**{k: [str(v)] for k, v in CAR.items()}, "origin": ["GB"]}
    assert result["settlement"] == settlement
    assert result["payment_status"] == expected
    assert len(result["routes"][0]["listings"]) == 3
    assert result["routes"][0]["listings"][0]["price_eur"] == 23000
    assert ("Settlement confirmed" in result["message"]) is (expected == "confirmed")


@pytest.mark.parametrize("error", [
    ValueError("HTTP 404: no data"), ValueError("HTTP 429: Retry-After: 200"),
    ValueError("Payment rejected"), httpx.ConnectError("connection refused"),
])
def test_helper_errors_propagate_without_retry(monkeypatch, error):
    calls = []

    def run(args, *, report_errors):
        calls.append(args)
        raise error

    monkeypatch.setattr(payment, "run", run)
    with pytest.raises(type(error), match=str(error)):
        assistant.compare_car(**CAR, full=True, pay=True)
    assert len(calls) == 1


@pytest.mark.parametrize("settlement", [{"success": False}, []])
def test_bad_settlement_not_success(monkeypatch, payload, settlement):
    monkeypatch.setattr(payment, "run", lambda *a, **kw: {
        "status": 200, "result": payload, "settlement": settlement,
    })
    with pytest.raises(ValueError, match="[Ss]ettlement"):
        assistant.compare_car(**CAR, full=True, pay=True)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://user:pass@example.org/car", "http://example.org/car"])
def test_listing_urls_validated_not_followed(monkeypatch, payload, url):
    payload["routes"][0]["cheapest_matching_listings"] = [{"url": url}]
    monkeypatch.setattr(payment, "run", lambda *a, **kw: {"status": 200, "result": payload})
    with pytest.raises(ValueError):
        assistant.compare_car(**CAR, full=True, pay=True)


def test_json_success_exactly_one_object(monkeypatch, payload, capsys):
    respond(monkeypatch, payload=payload)
    assert assistant.main([*FLAGS, "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["mode"] == "LIVE_FREE_PREVIEW"


def test_json_error_no_stdout(monkeypatch, capsys):
    respond(monkeypatch, status=429, headers={"Retry-After": "45"})
    assert assistant.main([*FLAGS, "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "HTTP 429" in captured.err and "Retry-After: 45" in captured.err


def test_full_json_no_nested_helper_stdout(monkeypatch, payload, capsys):
    def run(args, *, report_errors):
        assert report_errors is False
        return {"status": 200, "result": payload, "settlement": None}

    monkeypatch.setattr(payment, "run", run)
    assert assistant.main([*FLAGS, "--full", "--pay", "--json"]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["payment_status"] == "unconfirmed"
    assert len(captured.out.splitlines()) == 1


def test_interactive_explicit_free_comparison(monkeypatch, payload, capsys):
    answers = iter(["Volkswagen", "Golf", "2023", "50000", "Petrol", "125", "Manual", "GB", "compare"])
    monkeypatch.setattr(builtins, "input", lambda: next(answers))
    calls = respond(monkeypatch, payload=payload)
    assert assistant.main(["--interactive", "--json"]) == 0
    captured = capsys.readouterr()
    assert "Type 'compare'" in captured.err
    result = json.loads(captured.out)
    assert result["vehicle"]["co2_g_km"] == 125
    assert result["vehicle"]["transmission"] == "Manual"
    assert result["vehicle"]["origin"] == "GB"
    assert len(calls) == 1


def test_interactive_cancelled_does_not_request(monkeypatch, capsys):
    answers = iter(["", "", "no"])
    monkeypatch.setattr(builtins, "input", lambda: next(answers))
    assert assistant.main([*FLAGS, "--interactive", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cancelled" in captured.err


@pytest.mark.parametrize("flags", [
    ["--interactive", "--pay", "--full"], ["--interactive", "--full"],
    ["--pay"], ["--demo", "--pay", "--full"], ["--max-usdc", "NaN"], [],
])
def test_cli_invalid_flags_fail_without_request(flags, capsys):
    assert assistant.main(flags) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err


@pytest.mark.parametrize("invocation", [["-m", "irish_car_import_assistant"],
                                       ["-m", "irish_car_import_assistant.assistant"]])
def test_both_entrypoints_offline(invocation):
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run([sys.executable, *invocation, "--demo", "--json"],
                               cwd=root, capture_output=True, text=True, check=False, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["mode"] == "SYNTHETIC_OFFLINE_DEMO"
