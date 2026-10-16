"""Inspect an x402 challenge; only --pay authorizes one capped paid retry."""

import argparse
import ipaddress
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote, urlsplit

import httpx
from eth_account import Account
from x402 import x402ClientSync
from x402.http import x402HTTPClientSync
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.schemas.errors import PaymentError

DEFAULT_PATH = "/api/v1/a2a/vrt-estimate?price_eur=20000&co2_g_km=125&fuel_type=Diesel"
USDC_ASSETS = {
    "eip155:8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "eip155:84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}


def spend_cap(value: str) -> Decimal:
    try:
        cap = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("max-usdc must be a positive finite decimal") from exc
    if not cap.is_finite() or cap <= 0:
        raise argparse.ArgumentTypeError("max-usdc must be a positive finite decimal")
    return cap


def validate_origin(url: str):
    if any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url or "#" in url:
        raise ValueError("URL must not contain whitespace, backslashes or fragments")
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are forbidden")
    if not parsed.hostname or parsed.scheme not in ("http", "https"):
        raise ValueError("URL must have an HTTP(S) origin")
    local = parsed.hostname.lower() == "localhost"
    try:
        local = local or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass  # Non-IP hostnames are allowed over HTTPS only.
    if parsed.scheme == "http" and not local:
        raise ValueError("HTTP is allowed only for localhost or loopback IP addresses")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def request_url(base_url: str, path: str) -> str:
    validate_origin(base_url)
    base = urlsplit(base_url)
    if base.path not in ("", "/") or base.query or "?" in base_url:
        raise ValueError("base_url must be an origin without a path or query; use --path")
    decoded = unquote(path)
    if (
        not path.startswith("/") or decoded.startswith("//")
        or "\\" in decoded or "#" in path
        or any(c.isspace() or ord(c) < 32 for c in decoded.split("?", 1)[0])
        or any(ord(c) < 32 for c in decoded)
    ):
        raise ValueError("--path must be a local absolute path, not a URL, authority or fragment")
    parts = urlsplit(path)
    if parts.scheme or parts.netloc:
        raise ValueError("--path must stay on the base_url origin")
    url = base_url.rstrip("/") + path
    if validate_origin(url) != validate_origin(base_url):
        raise ValueError("--path must stay on the base_url origin")
    return url


def choose_requirement(required, cap: Decimal):
    if required.x402_version != 2:
        raise ValueError("Only x402 v2 exact USDC on Base or Base Sepolia is supported")
    cap_numerator, cap_denominator = cap.as_integer_ratio()
    candidates = []
    rejected = []
    for option in required.accepts:
        asset = USDC_ASSETS.get(option.network)
        if option.scheme != "exact" or asset is None or option.asset.lower() != asset.lower():
            rejected.append("unsupported scheme/network/asset")
            continue
        if not re.fullmatch(r"[0-9]+", option.amount) or int(option.amount) <= 0:
            rejected.append("invalid USDC atomic amount")
            continue
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", option.pay_to) or int(option.pay_to, 16) == 0:
            rejected.append("invalid payment recipient")
            continue
        # Compare integral atomic units directly, without float or Decimal rounding.
        if int(option.amount) * cap_denominator > cap_numerator * 1_000_000:
            rejected.append("price exceeds --max-usdc spend cap")
            continue
        candidates.append(option)
    if not candidates:
        raise ValueError("No safe payment requirement: " + "; ".join(rejected or ["empty accepts"]))
    return min(candidates, key=lambda option: int(option.amount))


def response_body(response: httpx.Response):
    try:
        return response.json()
    except json.JSONDecodeError:
        return response.text


def check_response(response: httpx.Response, *, paid: bool = False) -> None:
    status = response.status_code
    if 300 <= status < 400:
        raise ValueError(f"HTTP {status}: redirect refused (no redirects, including same-origin)")
    if status == 404:
        raise ValueError("HTTP 404: route or data not found; no settlement is confirmed")
    if status == 429:
        retry = response.headers.get("Retry-After", "not supplied")
        raise ValueError(f"HTTP 429: rate limited; wait before trying again. Retry-After: {retry}")
    if status == 402 and paid:
        raise ValueError(f"HTTP 402: payment rejected; no automatic retry: {response_body(response)}")
    if status >= 400:
        raise ValueError(f"HTTP {status}: request failed: {response_body(response)}")


def run(args, *, report_errors: bool = True) -> dict:
    url = request_url(args.base_url, args.path)
    client_name = getattr(args, "client_name", "x402-example-client")
    if not isinstance(client_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", client_name):
        raise ValueError("client_name must be a product name of 1-40 letters, digits, dots, underscores or hyphens")
    sdk = x402ClientSync()
    http = x402HTTPClientSync(sdk)
    with httpx.Client(follow_redirects=False, timeout=30,
                      headers={"X-Client-Name": client_name, "User-Agent": client_name + "/1.0"}) as transport:
        challenge = transport.get(url)
        if challenge.status_code != 402:
            check_response(challenge)
            return {
                "status": challenge.status_code, "result": response_body(challenge),
                "message": "No HTTP 402 challenge; no payment authorized",
                "settlement": None,
            }
        required = http.get_payment_required_response(
            challenge.headers.get,
            response_body(challenge),
        )
        option = choose_requirement(required, args.max_usdc)
        if required.resource and required.resource.url:
            if validate_origin(required.resource.url) != validate_origin(url):
                raise ValueError("Challenge resource URL must use the requested origin")
        result = {
            "status": 402,
            "challenge": required.model_dump(by_alias=True, exclude_none=True),
            "selected": option.model_dump(by_alias=True, exclude_none=True),
            "max_usdc": str(args.max_usdc),
            "settlement": None,
        }
        if not args.pay:
            result["message"] = "Inspection only: HTTP 402 payment required; use --pay to authorize one capped retry"
            return result

        key = os.environ.get("X402_CLIENT_PRIVATE_KEY")
        if not key:
            raise ValueError("--pay requires X402_CLIENT_PRIVATE_KEY (use a throwaway wallet)")
        account = Account.from_key(key)
        sdk.register(option.network, ExactEvmClientScheme(EthAccountSigner(account)))
        # Restrict SDK selection to the already validated, capped requirement.
        selected = required.model_copy(update={"accepts": [option]})
        headers = http.encode_payment_signature_header(sdk.create_payment_payload(selected))
        paid = transport.get(url, headers=headers, timeout=60)
        result.update(status=paid.status_code, result=response_body(paid))
        if paid.headers.get("PAYMENT-RESPONSE") or paid.headers.get("X-PAYMENT-RESPONSE"):
            settlement = http.get_payment_settle_response(paid.headers.get)
            result["settlement"] = settlement.model_dump(by_alias=True, exclude_none=True)
            if not settlement.success:
                if report_errors:
                    print(json.dumps(result))
                raise ValueError("Payment settlement failed; see settlement details above")
        else:
            result["message"] = "No settlement header present; settlement is not confirmed"
        if report_errors and (paid.is_error or paid.is_redirect):
            print(json.dumps(result))
        check_response(paid, paid=True)
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="HTTPS origin (HTTP only for localhost/loopback)")
    parser.add_argument("--path", default=DEFAULT_PATH, help="same-origin absolute path and optional query")
    parser.add_argument("--pay", action="store_true", help="authorize one signed, capped paid retry")
    parser.add_argument("--max-usdc", type=spend_cap, default=Decimal("0.02"), help="maximum USDC per authorization (default: 0.02)")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (ValueError, PaymentError, httpx.HTTPError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
