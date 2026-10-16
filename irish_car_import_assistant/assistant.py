"""Free-first, deterministic UK/NI-to-Ireland buyer comparison.

Run ``python -m irish_car_import_assistant --demo`` for synthetic offline data.
Agent builders can call compare_car(make=..., model=..., year=..., mileage_km=...,
fuel_type=..., co2_g_km=...) directly; payment requires full=True AND pay=True.
Only public HTTP endpoints are used. No server configuration or database imports.
"""

import argparse
import json
import math
import sys
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx

BASE_URL = "https://irish-auto-arbitrage-production.up.railway.app"
CLIENT_NAME = "car-import-assistant"
USER_AGENT = "car-import-assistant/1.0 (public HTTP buyer comparison)"
DISCLAIMER = (
    "Estimate only: advisory comparison, not tax, customs or financial advice or a "
    "guarantee. Asking prices are not sale prices. Verify eligibility, vehicle history, "
    "exchange rates, duty, VAT, Revenue OMSP/VRT and NOx before buying. No import is executed."
)
FUELS = {
    fuel.lower(): fuel for fuel in (
        "Petrol", "Diesel", "Electric", "Petrol Hybrid", "Diesel Hybrid",
        "Petrol Plug-in Hybrid", "Diesel Plug-in Hybrid",
    )
}
FUELS.update(ev="Electric", bev="Electric")
VERDICTS = {"IMPORT": "Import", "MARGINAL": "Marginal", "BUY_IN_IRELAND": "Buy in Ireland"}
COSTS = {
    "vehicle_price_eur": "Vehicle", "shipping_eur": "Transport",
    "customs_duty_eur": "Customs", "import_vat_eur": "VAT",
    "vrt_eur": "VRT", "nox_levy_eur": "NOx", "other_costs_eur": "Other",
}
REQUIRED = ("make", "model", "year", "mileage_km", "fuel_type", "co2_g_km")


def spend_cap(value):
    """Parse an exact positive cap; bools and non-finite/oversized values are invalid."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("max-usdc must be a positive finite decimal, at most 0.02 USDC")
    try:
        cap = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("max-usdc must be a positive finite decimal, at most 0.02 USDC") from exc
    if not cap.is_finite() or not 0 < cap <= Decimal("0.02"):
        raise ValueError("max-usdc must be a positive finite decimal, at most 0.02 USDC")
    return cap


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must be nonempty text without control characters")
    return value.strip()


def _integer(value, name, low, high=None):
    if type(value) is not int or value < low or (high is not None and value > high):
        raise ValueError(f"{name} must be an integer in {low}..{high or 'unbounded'}")
    return value


def _vehicle(make, model, year, mileage_km, fuel_type, co2_g_km, transmission, origin):
    supplied = dict(zip(REQUIRED, (make, model, year, mileage_km, fuel_type, co2_g_km)))
    missing = [key.replace("_", "-") for key, value in supplied.items() if value is None]
    if missing:
        raise ValueError("Missing required live fields: " + ", ".join(missing))
    fuel = _text(fuel_type, "fuel-type").lower()
    if fuel not in FUELS:
        raise ValueError("fuel-type must be Petrol, Diesel, Electric, Petrol Hybrid, Diesel Hybrid, "
                         "Petrol Plug-in Hybrid or Diesel Plug-in Hybrid; specify the hybrid's fuel.")
    origin = _text(origin, "origin").upper()
    if origin not in ("ANY", "GB", "NI"):
        raise ValueError("origin must be ANY, GB or NI")
    vehicle = {
        "make": _text(make, "make"), "model": _text(model, "model"),
        "year": _integer(year, "year", 1950, 2100),
        "mileage_km": _integer(mileage_km, "mileage-km", 0),
        "fuel_type": FUELS[fuel], "co2_g_km": _integer(co2_g_km, "co2-g-km", 0, 1000),
        "origin": origin,
    }
    if transmission is not None:
        transmission = _text(transmission, "transmission").title()
        if transmission not in ("Manual", "Automatic"):
            raise ValueError("transmission must be Manual or Automatic")
        vehicle["transmission"] = transmission
    return vehicle


def _mapping(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"Malformed response: {name} must be an object")
    return value


def _number(value, name, *, nonnegative=False, count=False):
    if (
        type(value) not in (int, float) or (type(value) is float and not math.isfinite(value))
        or (nonnegative and value < 0)
        or (count and (type(value) is not int or value <= 0))
    ):
        raise ValueError(f"Malformed response: {name} must be a finite "
                         + ("positive integer" if count else "nonnegative number" if nonnegative else "number"))
    return value


def _recommendation(value):
    if not isinstance(value, str) or value not in VERDICTS:
        raise ValueError("Malformed response: unknown recommendation")
    return value


def _comparison(payload, *, full, validate_url):
    data = _mapping(payload, "comparison")
    market = _mapping(data.get("irish_market"), "irish_market")
    median = _number(market.get("median_price_eur"), "Irish median", nonnegative=True)
    count = _number(market.get("comparable_count"), "Irish comparable_count", count=True)
    verdict = _mapping(data.get("verdict"), "verdict")
    recommendation = _recommendation(verdict.get("recommendation"))
    saving = _number(verdict.get("saving_eur"), "verdict saving")
    routes = data.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ValueError("Malformed response: routes must be a nonempty list")
    normalized = []
    for route in routes:
        route = _mapping(route, "route")
        origin = route.get("origin")
        if not isinstance(origin, str) or origin not in ("GB", "NI") or any(r["origin"] == origin for r in normalized):
            raise ValueError("Malformed response: route origin must be unique GB or NI")
        costs = _mapping(route.get("landed_cost"), "landed_cost")
        costs = {key: _number(costs.get(key), key, nonnegative=True)
                 for key in (*COSTS, "total_landed_cost_eur")}
        total = costs["total_landed_cost_eur"]
        route_saving = _number(route.get("saving_vs_irish_median_eur"), "route saving")
        if abs(sum(costs[key] for key in COSTS) - total) > 0.05 or abs(median - total - route_saving) > 0.05:
            raise ValueError("Malformed response: inconsistent landed costs or saving")
        row = {
            "origin": origin,
            "comparable_count": _number(route.get("comparable_count"), "route comparable_count", count=True),
            "landed_cost": costs, "saving_vs_irish_median_eur": route_saving,
            "recommendation": _recommendation(route.get("recommendation")),
        }
        if full and "cheapest_matching_listings" in route:
            listings = route["cheapest_matching_listings"]
            if not isinstance(listings, list):
                raise ValueError("Malformed response: cheapest_matching_listings must be a list")
            row["listings"] = []
            for listing in listings[:3]:
                listing = _mapping(listing, "listing")
                url = _text(listing.get("url"), "listing URL")
                validate_url(url)
                details = {"url": url}
                for key in ("make", "model", "fuel_type", "transmission", "currency"):
                    if listing.get(key) is not None:
                        details[key] = _text(listing[key], f"listing {key}")
                for key in ("price_eur", "price_local", "landed_cost_eur", "mileage_km", "year"):
                    if listing.get(key) is not None:
                        details[key] = _number(listing[key], f"listing {key}", nonnegative=True)
                row["listings"].append(details)
        normalized.append(row)
    best = max(normalized, key=lambda route: route["saving_vs_irish_median_eur"])
    if (verdict.get("best_origin") != best["origin"] or abs(saving - best["saving_vs_irish_median_eur"]) > 0.05
            or recommendation != best["recommendation"]):
        raise ValueError("Malformed response: verdict does not match best route")
    assumptions = data.get("assumptions")
    if not isinstance(assumptions, list) or not all(isinstance(item, str) and item.strip() for item in assumptions):
        raise ValueError("Malformed response: assumptions must be a list of text")
    if data.get("estimate_only") is not True:
        raise ValueError("Malformed response: estimate_only must be true")
    return {
        "irish_market": {"median_price_eur": median, "comparable_count": count},
        "verdict": {"recommendation": recommendation, "label": VERDICTS[recommendation],
                    "best_origin": best["origin"], "saving_eur": saving},
        "routes": normalized, "assumptions": assumptions, "estimate_only": True,
    }


def _check_http(response):
    status = response.status_code
    if 300 <= status < 400:
        raise ValueError(f"HTTP {status}: redirect refused; no payment or automatic retry")
    if status == 404:
        raise ValueError("HTTP 404: no matching data. Broaden filters: omit transmission, check make/model "
                         "spelling or try origin ANY. Do not invent CO2. No automatic paid upgrade.")
    if status == 429:
        retry = response.headers.get("Retry-After", "not supplied")
        raise ValueError(f"HTTP 429: quota/rate limit reached; Retry-After: {retry}. No automatic paid upgrade.")
    if status >= 500:
        raise ValueError(f"HTTP {status}: comparison service unavailable; no automatic retry")
    if status >= 400:
        raise ValueError(f"HTTP {status}: comparison request failed; no automatic retry or payment upgrade")
    if status != 200:
        raise ValueError(f"Unexpected HTTP {status}; expected a comparison object")


def _demo():
    vehicle = _vehicle("Volkswagen", "Golf", 2023, 50000, "Petrol", 125, None, "GB")
    payload = {
        "irish_market": {"median_price_eur": 29950, "comparable_count": 68},
        "verdict": {"recommendation": "BUY_IN_IRELAND", "best_origin": "GB", "saving_eur": -8060.60},
        "routes": [{
            "origin": "GB", "comparable_count": 23, "recommendation": "BUY_IN_IRELAND",
            "saving_vs_irish_median_eur": -8060.60,
            "landed_cost": dict(zip((*COSTS, "total_landed_cost_eur"),
                                   (23218.43, 650, 2386.84, 6038.71, 5016.62, 200, 500, 38010.60))),
        }],
        "assumptions": [
            "SYNTHETIC_OFFLINE_DEMO: illustrative figures and counts, not live findings or real cars.",
            "GB vehicle price converted to EUR; transport 650; duty 10% of vehicle + transport; "
            "VAT 23% of vehicle + transport + duty (rounded).",
            "Irish median used as an OMSP proxy; illustrative VRT 5016.62; NOx 200; other costs 500.",
        ],
        "estimate_only": True,
    }
    return {
        "mode": "SYNTHETIC_OFFLINE_DEMO", "vehicle": vehicle,
        **_comparison(payload, full=False, validate_url=None),
        "payment_status": "not_authorized", "settlement": None,
        "listings_note": "Synthetic example only; no real listings. Individual listings reserved for the full paid call.",
        "disclaimer": DISCLAIMER,
    }


def compare_car(*, make=None, model=None, year=None, mileage_km=None, fuel_type=None,
                co2_g_km=None, transmission=None, origin="ANY", base_url=BASE_URL,
                demo=False, full=False, pay=False, max_usdc="0.02"):
    """Return a validated dict. No spending without explicit full=True and pay=True.

    Raises ValueError for invalid input, HTTP/payload/payment failures and httpx.HTTPError
    for transport failures. A full authorization delegates one capped replay to the shared
    x402 client; no retry loop or automatic paid upgrade exists.
    """
    if any(type(flag) is not bool for flag in (demo, full, pay)):
        raise ValueError("demo, full and pay must be booleans")
    cap = spend_cap(max_usdc)
    if pay and (not full or demo):
        raise ValueError("--pay requires --full and cannot be used with --demo")
    if demo:
        return _demo()
    vehicle = _vehicle(make, model, year, mileage_km, fuel_type, co2_g_km, transmission, origin)
    from . import payments as payment

    path = "/api/v1/a2a/" + ("uk-import-check" if full else "uk-import-preview")
    path += "?" + urlencode(vehicle)
    url = payment.request_url(base_url, path)
    settlement = None
    payment_status = "not_authorized"
    if full:
        args = argparse.Namespace(base_url=base_url, path=path, pay=pay, max_usdc=cap,
                                  client_name=CLIENT_NAME)
        try:
            result = payment.run(args, report_errors=False)
        except payment.PaymentError as exc:
            raise ValueError(f"Payment error; no automatic retry: {exc}") from exc
        except ValueError as exc:
            if str(exc).startswith("HTTP 404"):
                raise ValueError(f"{exc}. Broaden filters: omit transmission, check make/model spelling "
                                 "or try origin ANY. No automatic retry.") from exc
            raise
        result = _mapping(result, "payment helper result")
        if result.get("status") == 402 and not pay:
            return {
                "mode": "LIVE_QUOTE_INSPECTION", "vehicle": vehicle,
                "payment_status": "not_authorized", "settlement": None,
                "max_usdc": str(cap), "quote": _mapping(result.get("selected"), "payment quote"),
                "message": "No payment made. Re-run the same vehicle with --full --pay to authorize "
                           "one capped replay (maximum " + str(cap) + " USDC).",
                "disclaimer": DISCLAIMER,
            }
        if result.get("status") != 200:
            raise ValueError(f"Payment/full request failed (HTTP {result.get('status')}); no automatic retry")
        payload = result.get("result")
        settlement = result.get("settlement")
        if settlement is not None:
            settlement = _mapping(settlement, "settlement")
            if settlement.get("success") is False:
                raise ValueError("Payment settlement failed; no automatic retry")
        payment_status = ("confirmed" if pay and settlement is not None and settlement.get("success") is True
                          else "unconfirmed" if pay else "not_authorized")
    else:
        with httpx.Client(follow_redirects=False, timeout=30,
                          headers={"X-Client-Name": CLIENT_NAME, "User-Agent": USER_AGENT}) as client:
            response = client.get(url)
        _check_http(response)
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Malformed JSON from comparison service") from exc
    comparison = _comparison(payload, full=full, validate_url=payment.validate_origin)
    if vehicle["origin"] != "ANY" and any(r["origin"] != vehicle["origin"] for r in comparison["routes"]):
        raise ValueError("Malformed response: routes do not match requested origin")
    return {
        "mode": "LIVE_FULL" if full and pay else "LIVE_QUOTE_INSPECTION" if full else "LIVE_FREE_PREVIEW",
        "vehicle": vehicle, **comparison,
        "payment_status": payment_status, "settlement": settlement,
        "message": ("Settlement confirmed by payment helper." if payment_status == "confirmed"
                    else "Settlement unconfirmed; do not assume payment completed." if pay
                    else "No payment made or authorized."),
        "listings_note": ("Listing links are unvisited public source links; verify availability and details."
                          if full else "Individual listings reserved for the full paid call (--full --pay)."),
        "disclaimer": DISCLAIMER,
    }


def format_comparison(result):
    vehicle = result["vehicle"]
    lines = [result["mode"], "Requested vehicle: " + ", ".join(f"{k}={v}" for k, v in vehicle.items())]
    if "quote" in result:
        lines.append("Unpaid quote (USDC atomic units): " + json.dumps(result["quote"]))
    if "verdict" in result:
        market = result["irish_market"]
        lines += [f"Verdict: {result['verdict']['label']}",
                  f"Irish median: EUR {market['median_price_eur']:,.2f} ({market['comparable_count']} comparables)"]
        for route in result["routes"]:
            saving = route["saving_vs_irish_median_eur"]
            lines.append(f"{route['origin']}: landed EUR {route['landed_cost']['total_landed_cost_eur']:,.2f}; "
                         f"{'saving' if saving >= 0 else 'shortfall'} EUR {abs(saving):,.2f} "
                         f"({route['comparable_count']} comparables)")
            lines.append("  " + "; ".join(f"{label} EUR {route['landed_cost'][key]:,.2f}"
                                         for key, label in COSTS.items()))
            for listing in route.get("listings", []):
                lines.append("  Listing: " + ", ".join(f"{k}={v}" for k, v in listing.items()))
        lines += ["Assumptions:"] + ["  " + item for item in result["assumptions"]]
    lines += [result.get("message", ""), result.get("listings_note", ""), result["disclaimer"]]
    return "\n".join(line for line in lines if line)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="synthetic offline fixture; no network or wallet")
    parser.add_argument("--interactive", action="store_true", help="enter a vehicle, then explicitly run a free comparison")
    for name in REQUIRED:
        parser.add_argument("--" + name.replace("_", "-"), type=int if name in ("year", "mileage_km", "co2_g_km") else str)
    parser.add_argument("--transmission")
    parser.add_argument("--origin", default="ANY")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--full", action="store_true", help="inspect full-call quote without payment unless --pay")
    parser.add_argument("--pay", action="store_true", help="explicitly authorize one capped full-call payment replay")
    parser.add_argument("--max-usdc", default="0.02")
    parser.add_argument("--json", action="store_true", help="one JSON object on stdout; errors on stderr")
    args = parser.parse_args(argv)
    try:
        spend_cap(args.max_usdc)
        if args.pay and (not args.full or args.demo):
            raise ValueError("--pay requires --full and cannot be used with --demo")
        if args.interactive:
            if args.demo or args.full or args.pay:
                raise ValueError("--interactive is free-only; do not combine with --demo, --full or --pay")
            for name in REQUIRED:
                if getattr(args, name) is None:
                    print(name.replace("_", "-") + ": ", end="", file=sys.stderr, flush=True)
                    answer = input()
                    if name in ("year", "mileage_km", "co2_g_km"):
                        answer = int(answer)
                    setattr(args, name, answer)
            if args.transmission is None:
                print("transmission (Manual/Automatic, blank for either): ", end="", file=sys.stderr, flush=True)
                args.transmission = input().strip() or None
            print("origin (ANY/GB/NI; blank for ANY): ", end="", file=sys.stderr, flush=True)
            args.origin = input().strip() or args.origin
            _vehicle(*(getattr(args, name) for name in REQUIRED), args.transmission, args.origin)
            print("Type 'compare' to run the FREE public comparison (no payment): ",
                  end="", file=sys.stderr, flush=True)
            if input().strip().lower() != "compare":
                raise ValueError("Free comparison cancelled; no request or payment made")
        options = vars(args).copy()
        options.pop("interactive")
        as_json = options.pop("json")
        result = compare_car(**options)
    except (ValueError, httpx.HTTPError, EOFError) as exc:
        print(f"car-import-assistant: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, allow_nan=False) if as_json else format_comparison(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
