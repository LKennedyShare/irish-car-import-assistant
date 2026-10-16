import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from irish_car_import_assistant import payments as client

ORIGIN = "https://example.test"


def encoded(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


def requirement(**changes):
    return {
        "scheme": "exact",
        "network": "eip155:8453",
        "asset": client.USDC_ASSETS["eip155:8453"],
        "amount": "5000",
        "payTo": "0x" + "12" * 20,
        "maxTimeoutSeconds": 60,
        "extra": {"name": "USD Coin", "version": "2"},
        **changes,
    }


def challenge(*options, resource=ORIGIN + client.DEFAULT_PATH):
    return httpx.Response(
        402,
        headers={"PAYMENT-REQUIRED": encoded({
            "x402Version": 2, "resource": {"url": resource},
            "accepts": list(options) or [requirement()],
        })},
        json={"error": "Payment required"},
    )


@pytest.fixture
def mock_http(monkeypatch):
    real_client = httpx.Client
    requests = []

    def install(*responses):
        queued = iter(responses)

        def handler(request):
            requests.append(request)
            response = next(queued)
            if isinstance(response, httpx.HTTPError):
                raise response
            return response

        monkeypatch.setattr(
            client.httpx, "Client",
            lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
        )
        return requests

    return install


@pytest.fixture
def fake_payment(monkeypatch):
    state = {"payloads": [], "networks": []}
    monkeypatch.setenv("X402_CLIENT_PRIVATE_KEY", "fake-never-signed")
    monkeypatch.setattr(client.Account, "from_key", lambda key: SimpleNamespace(address="fake"))
    monkeypatch.setattr(client, "EthAccountSigner", lambda account: account)
    monkeypatch.setattr(client, "ExactEvmClientScheme", lambda signer: signer)

    def register(self, network, scheme):
        state["networks"].append(network)
        return self

    def payload(self, required):
        state["payloads"].append(required)
        return {"fake": True}

    monkeypatch.setattr(client.x402ClientSync, "register", register)
    monkeypatch.setattr(client.x402ClientSync, "create_payment_payload", payload)
    monkeypatch.setattr(
        client.x402HTTPClientSync, "encode_payment_signature_header",
        lambda self, payload: {"PAYMENT-SIGNATURE": "fake-signature"},
    )
    return state


def test_inspection_needs_no_key_and_never_signs(mock_http, monkeypatch, capsys):
    monkeypatch.delenv("X402_CLIENT_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(
        client.x402ClientSync, "create_payment_payload",
        lambda *args: pytest.fail("Inspection must never sign"),
    )
    requests = mock_http(challenge())
    assert client.main([ORIGIN]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == 402
    assert "Inspection only" in output["message"]
    assert output["settlement"] is None
    assert output["selected"]["amount"] == "5000"
    assert len(requests) == 1
    assert "PAYMENT-SIGNATURE" not in requests[0].headers
    assert requests[0].url.path == "/api/v1/a2a/vrt-estimate"


def test_explicit_pay_missing_key(mock_http, monkeypatch, capsys):
    monkeypatch.delenv("X402_CLIENT_PRIVATE_KEY", raising=False)
    requests = mock_http(challenge())
    assert client.main([ORIGIN, "--pay"]) == 1
    assert "--pay requires X402_CLIENT_PRIVATE_KEY" in capsys.readouterr().err
    assert len(requests) == 1


def test_successful_capped_selection_and_settlement(mock_http, fake_payment, capsys):
    settled = {"success": True, "transaction": "0xfake", "network": "eip155:8453"}
    requests = mock_http(
        challenge(
            requirement(amount="50000"), requirement(amount="18000"),
            requirement(amount="5000"), requirement(network="eip155:1", amount="1"),
        ),
        httpx.Response(200, json={"vrt_eur": 123}, headers={"PAYMENT-RESPONSE": encoded(settled)}),
    )
    assert client.main([ORIGIN, "--pay", "--max-usdc", "0.02"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["result"] == {"vrt_eur": 123}
    assert output["settlement"]["transaction"] == "0xfake"
    assert fake_payment["networks"] == ["eip155:8453"]
    assert [o.amount for o in fake_payment["payloads"][0].accepts] == ["5000"]
    assert len(requests) == 2
    assert requests[1].headers["PAYMENT-SIGNATURE"] == "fake-signature"
    assert requests[0].url == requests[1].url


def test_default_with_key_still_does_not_pay(mock_http, fake_payment):
    requests = mock_http(challenge())
    assert client.main([ORIGIN]) == 0
    assert len(requests) == 1
    assert not fake_payment["payloads"]


@pytest.mark.parametrize("changes,reason", [
    ({"network": "eip155:1"}, "unsupported"),
    ({"network": "base"}, "unsupported"),
    ({"asset": "0x" + "34" * 20}, "unsupported"),
    ({"scheme": "upto"}, "unsupported"),
    ({"amount": "20001"}, "spend cap"),
    ({"amount": "-1"}, "invalid"),
    ({"amount": "0"}, "invalid"),
    ({"amount": "5e3"}, "invalid"),
    ({"payTo": "https://evil.test"}, "invalid"),
    ({"payTo": "0x" + "00" * 20}, "invalid"),
])
def test_unsafe_requirement_never_signs(changes, reason, mock_http, fake_payment, capsys):
    requests = mock_http(challenge(requirement(**changes)))
    assert client.main([ORIGIN, "--pay"]) == 1
    assert reason in capsys.readouterr().err
    assert not fake_payment["payloads"]
    assert len(requests) == 1


def test_cap_has_no_decimal_rounding_loophole(mock_http, fake_payment, capsys):
    mock_http(challenge())
    assert client.main([ORIGIN, "--pay", "--max-usdc", "0.004999999999999999999999999999999"]) == 1
    assert "spend cap" in capsys.readouterr().err
    assert not fake_payment["payloads"]


@pytest.mark.parametrize("network", list(client.USDC_ASSETS))
def test_allowlisted_networks(network, mock_http, fake_payment):
    mock_http(
        challenge(requirement(network=network, asset=client.USDC_ASSETS[network].lower())),
        httpx.Response(200, json={"ok": True}),
    )
    assert client.main([ORIGIN, "--pay"]) == 0
    assert fake_payment["networks"] == [network]


@pytest.mark.parametrize("path", [
    "https://evil.test/pay", "//evil.test/pay", "relative/path",
    "/route#fragment", "/\\evil.test", "/%2Fevil.test", "/%5Cevil.test",
    "/%0Aroute", "/ route",
])
def test_invalid_paths_never_request(path, mock_http, fake_payment, capsys):
    requests = mock_http()
    assert client.main([ORIGIN, "--path", path, "--pay"]) == 1
    assert capsys.readouterr().err
    assert requests == []
    assert not fake_payment["payloads"]


@pytest.mark.parametrize("origin", [
    "http://example.test", "https://user:password@example.test",
    "https://example.test#fragment", "https://example.test/path",
    "https://example.test?query", "file:///etc/passwd",
])
def test_unsafe_origins_never_request(origin, mock_http, capsys):
    requests = mock_http()
    assert client.main([origin]) == 1
    assert capsys.readouterr().err
    assert not requests


@pytest.mark.parametrize("origin", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"])
def test_local_http_allowed(origin):
    assert client.request_url(origin, "/test?q=yes") == origin + "/test?q=yes"


@pytest.mark.parametrize("paid", [False, True])
@pytest.mark.parametrize("location", ["https://evil.test/steal", ORIGIN + "/moved"])
def test_redirect_refusal(paid, location, mock_http, fake_payment, capsys):
    redirect = httpx.Response(307, headers={"location": location})
    requests = mock_http(*([challenge(), redirect] if paid else [redirect]))
    assert client.main([ORIGIN, "--pay"]) == 1
    assert "redirect refused" in capsys.readouterr().err
    assert len(requests) == (2 if paid else 1)
    assert all(request.url.host == "example.test" for request in requests)


@pytest.mark.parametrize("resource", ["https://evil.test/route", "http://example.test/route", "https://user@example.test/route"])
def test_unsafe_resource_never_signs(resource, mock_http, fake_payment, capsys):
    mock_http(challenge(resource=resource))
    assert client.main([ORIGIN, "--pay"]) == 1
    assert capsys.readouterr().err
    assert not fake_payment["payloads"]


@pytest.mark.parametrize("status,message", [(404, "not found"), (429, "rate limited"), (500, "request failed")])
@pytest.mark.parametrize("paid", [False, True])
def test_http_errors(status, message, paid, mock_http, fake_payment, capsys):
    response = httpx.Response(status, json={"detail": "test error"})
    requests = mock_http(*([challenge(), response] if paid else [response]))
    assert client.main([ORIGIN, "--pay"]) == 1
    assert message in capsys.readouterr().err
    assert len(requests) == (2 if paid else 1)


def test_payment_rejection_no_retry(mock_http, fake_payment, capsys):
    requests = mock_http(challenge(), httpx.Response(402, json={"error": "insufficient_funds"}))
    assert client.main([ORIGIN, "--pay"]) == 1
    assert "insufficient_funds" in capsys.readouterr().err
    assert len(requests) == 2


def test_no_settlement_does_not_claim_success(mock_http, fake_payment, capsys):
    mock_http(challenge(), httpx.Response(200, json={"ok": True}))
    assert client.main([ORIGIN, "--pay"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["settlement"] is None
    assert "settlement is not confirmed" in output["message"]


@pytest.mark.parametrize("header", ["PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE"])
def test_failed_settlement_reported(header, mock_http, fake_payment, capsys):
    mock_http(challenge(), httpx.Response(200, json={"ok": True}, headers={header: encoded({
        "success": False, "transaction": "", "network": "eip155:8453", "errorReason": "failed",
    })}))
    assert client.main([ORIGIN, "--pay"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["settlement"]["success"] is False
    assert "settlement failed" in captured.err


def test_malformed_settlement_not_silently_swallowed(mock_http, fake_payment, capsys):
    mock_http(challenge(), httpx.Response(200, json={"ok": True}, headers={"PAYMENT-RESPONSE": "invalid"}))
    assert client.main([ORIGIN, "--pay"]) == 1
    assert capsys.readouterr().err


def test_malformed_challenge(mock_http, capsys):
    mock_http(httpx.Response(402, json={"error": "no x402 challenge"}))
    assert client.main([ORIGIN]) == 1
    assert "payment required response" in capsys.readouterr().err


def test_legacy_challenge_rejected_without_signing(mock_http, fake_payment, capsys):
    mock_http(httpx.Response(402, json={
        "x402Version": 1, "accepts": [{
            "scheme": "exact", "network": "base", "maxAmountRequired": "5000",
            "resource": ORIGIN + client.DEFAULT_PATH, "description": "VRT",
            "mimeType": "application/json", "payTo": "0x" + "12" * 20,
            "maxTimeoutSeconds": 60, "asset": client.USDC_ASSETS["eip155:8453"],
        }],
    }))
    assert client.main([ORIGIN, "--pay"]) == 1
    assert "Only x402 v2" in capsys.readouterr().err
    assert not fake_payment["payloads"]


def test_network_error(mock_http, capsys):
    mock_http(httpx.ConnectError("offline"))
    assert client.main([ORIGIN]) == 1
    assert "offline" in capsys.readouterr().err


@pytest.mark.parametrize("cap", ["NaN", "Infinity", "-1", "0", "invalid"])
def test_invalid_cap_rejected(cap, mock_http):
    requests = mock_http()
    with pytest.raises(SystemExit) as error:
        client.main([ORIGIN, "--max-usdc", cap])
    assert error.value.code == 2
    assert not requests


def test_non_payment_success_is_reported_without_signing(mock_http, fake_payment, capsys):
    requests = mock_http(httpx.Response(200, json={"free": True}))
    assert client.main([ORIGIN, "--pay"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == {"free": True}
    assert len(requests) == 1
    assert not fake_payment["payloads"]
