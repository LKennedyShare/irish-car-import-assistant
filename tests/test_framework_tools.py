"""Real SDK tool invocation, with all external HTTP/payment operations mocked."""

import asyncio
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx
import pytest

from irish_car_import_assistant import CarImportTools, assistant
from irish_car_import_assistant import payments
from irish_car_import_assistant import tools as adapter


CAR = {
    "make": "Volkswagen", "model": "Golf", "year": 2023,
    "mileage_km": 50000, "fuel_type": "Petrol", "co2_g_km": 125,
    "transmission": None, "origin": "ANY",
}
VEHICLE = {key: value for key, value in CAR.items() if value is not None}
REAL_CLIENT = httpx.Client
REAL_SEND = httpx.Client.send


@pytest.fixture(autouse=True)
def no_external_operations(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected external HTTP, signing or wallet operation")

    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("X402_CLIENT_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(payments, "run", forbidden)
    monkeypatch.setattr(payments.Account, "from_key", forbidden)
    monkeypatch.setattr(payments, "x402ClientSync", forbidden)


@pytest.fixture(params=["langchain", "openai"])
def framework(request, monkeypatch):
    if request.param == "langchain":
        pytest.importorskip("langchain_core")
    else:
        pytest.importorskip("agents")
    # New SDKs can use httpx2 internally; prohibit those calls as well.
    try:
        import httpx2
    except ModuleNotFoundError:
        pass
    else:
        def forbidden(*args, **kwargs):
            pytest.fail("Unexpected SDK network operation")
        monkeypatch.setattr(httpx2.Client, "send", forbidden)
        monkeypatch.setattr(httpx2.AsyncClient, "send", forbidden)
    return request.param


def wrapped(session, framework):
    factory = getattr(session, f"as_{framework}_tools")
    return {tool.name: tool for tool in factory()}


async def ainvoke(tool, framework, arguments):
    if framework == "langchain":
        return await tool.ainvoke(arguments)
    from agents.tool_context import ToolContext

    raw = json.dumps(arguments)
    context = ToolContext(context=None, tool_name=tool.name,
                          tool_call_id="mock-call", tool_arguments=raw)
    return await tool.on_invoke_tool(context, raw)


def invoke(tool, framework, arguments):
    if framework == "langchain":
        return tool.invoke(arguments)
    return asyncio.run(ainvoke(tool, framework, arguments))


def successful_payment(monkeypatch, *, inspect=None, settlement=None):
    calls = []
    payload = assistant.compare_car(demo=True)

    def run(args, **kwargs):
        calls.append(args)
        if inspect:
            inspect(args)
        return {"status": 200, "result": payload, "settlement": settlement}

    monkeypatch.setattr(payments, "run", run)
    return calls


def free_http(monkeypatch):
    calls = []
    payload = assistant.compare_car(demo=True)

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload)

    def client(**kwargs):
        instance = REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)
        instance.send = REAL_SEND.__get__(instance, REAL_CLIENT)
        return instance

    monkeypatch.setattr(assistant.httpx, "Client", client)
    return calls


def test_real_objects_default_tools_and_offline(framework, monkeypatch):
    session = CarImportTools()
    tools = wrapped(session, framework)
    assert set(tools) == {"offline_demo", "preview", "quote"}
    if framework == "langchain":
        from langchain_core.tools import StructuredTool
        assert all(isinstance(tool, StructuredTool) for tool in tools.values())
    else:
        from agents import FunctionTool
        assert all(isinstance(tool, FunctionTool) for tool in tools.values())

    original = os.environ.__class__.get

    def no_keys(environ, key, *args):
        assert key not in {"X402_CLIENT_PRIVATE_KEY", "OPENAI_API_KEY"}
        return original(environ, key, *args)

    monkeypatch.setattr(os.environ.__class__, "get", no_keys)
    result = invoke(tools["offline_demo"], framework, {})
    assert result["mode"] == "SYNTHETIC_OFFLINE_DEMO"
    assert result["settlement"] is None
    assert json.loads(json.dumps(result)) == result
    assert session.reserved_calls == 0


def test_preview_uses_core_http_no_wallet_or_payment(framework, monkeypatch):
    session = CarImportTools()
    tool = wrapped(session, framework)["preview"]
    calls = free_http(monkeypatch)
    original = os.environ.__class__.get

    def no_keys(environ, key, *args):
        assert key not in {"X402_CLIENT_PRIVATE_KEY", "OPENAI_API_KEY"}
        return original(environ, key, *args)

    monkeypatch.setattr(os.environ.__class__, "get", no_keys)
    result = invoke(tool, framework, CAR)
    assert result["mode"] == "LIVE_FREE_PREVIEW"
    assert len(calls) == 1
    assert calls[0].url.path.endswith("/uk-import-preview")
    assert "payment-signature" not in calls[0].headers
    assert result["payment_status"] == "not_authorized"
    assert session.reserved_usdc == 0


def test_quote_never_authorizes_payment(framework, monkeypatch):
    session = CarImportTools(approval_policy=lambda vehicle: pytest.fail("Quote sought approval"))
    calls = []

    def quote(args, **kwargs):
        calls.append(args)
        assert args.pay is False
        return {"status": 402, "selected": {"amount": "20000"}}

    monkeypatch.setattr(payments, "run", quote)
    result = invoke(wrapped(session, framework)["quote"], framework, CAR)
    assert result["mode"] == "LIVE_QUOTE_INSPECTION"
    assert result["payment_status"] == "not_authorized"
    assert len(calls) == 1
    assert session.reserved_calls == 0


def test_paid_approval_normalized_immutable_and_same_instance_cap(framework, monkeypatch):
    seen = []

    def policy(vehicle):
        seen.append(dict(vehicle))
        with pytest.raises(TypeError):
            vehicle["make"] = "Not approved"
        copy = dict(vehicle)
        copy["model"] = "Cannot change request"
        return dict(vehicle) == VEHICLE

    session = CarImportTools(approval_policy=policy)
    tools = wrapped(session, framework)
    calls = successful_payment(monkeypatch)
    inputs = {**CAR, "make": " Volkswagen ", "fuel_type": "petrol", "origin": "any"}
    result = invoke(tools["paid_full"], framework, inputs)
    assert seen == [VEHICLE]
    assert result["vehicle"] == VEHICLE
    assert result["payment_status"] == "unconfirmed"
    assert len(calls) == 1 and calls[0].pay is True
    assert calls[0].max_usdc == Decimal("0.02")
    assert session.reserved_calls == 1
    assert session.reserved_usdc == Decimal("0.02")
    # Rebuilding either adapter must not reset the shared object's budget.
    with pytest.raises(PermissionError, match="budget exhausted"):
        invoke(wrapped(session, framework)["paid_full"], framework, CAR)
    assert len(calls) == 1


@pytest.mark.parametrize("decision", [False, None, 1, "True", {"approved": True}])
def test_policy_requires_literal_true(framework, decision):
    session = CarImportTools(approval_policy=lambda vehicle: decision)
    with pytest.raises(PermissionError, match="denied"):
        invoke(wrapped(session, framework)["paid_full"], framework, CAR)
    assert session.reserved_calls == 0
    assert session.reserved_usdc == 0


def test_invalid_vehicle_never_reaches_policy(framework):
    session = CarImportTools(approval_policy=lambda vehicle: pytest.fail("Invalid approval"))
    with pytest.raises(ValueError):
        invoke(wrapped(session, framework)["paid_full"], framework, {**CAR, "year": 1949})
    assert session.reserved_calls == 0


@pytest.mark.parametrize("field,value", [
    ("pay", True), ("max_usdc", "0.02"), ("base_url", "https://untrusted.example"),
    ("wallet", "secret"), ("approval", True), ("full", True),
    ("max_paid_calls", 999), ("year", "2023"), ("mileage_km", True),
])
def test_strict_schema_and_runtime_reject_model_authorization(framework, field, value):
    session = CarImportTools(approval_policy=lambda vehicle: pytest.fail("Invalid approval"))
    tools = wrapped(session, framework)
    for name, tool in tools.items():
        schema = (tool.args_schema.model_json_schema() if framework == "langchain"
                  else tool.params_json_schema)
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == (set(CAR) if name != "offline_demo" else set())
    with pytest.raises((ValueError, TypeError)):
        invoke(tools["paid_full"], framework, {**CAR, field: value})
    assert session.reserved_calls == 0


@pytest.mark.parametrize("failure", ["timeout", "http", "settlement"])
def test_failures_consume_reservation_no_retries(framework, monkeypatch, failure):
    session = CarImportTools(approval_policy=lambda vehicle: True)
    tool = wrapped(session, framework)["paid_full"]
    calls = []

    def fail(args, **kwargs):
        calls.append(args)
        if failure == "timeout":
            raise httpx.ReadTimeout("Unknown settlement")
        if failure == "http":
            return {"status": 503}
        return {"status": 200, "settlement": {"success": False}}

    monkeypatch.setattr(payments, "run", fail)
    with pytest.raises((ValueError, httpx.HTTPError)):
        invoke(tool, framework, CAR)
    with pytest.raises(PermissionError, match="budget exhausted"):
        invoke(tool, framework, CAR)
    assert len(calls) == 1
    assert session.reserved_usdc == Decimal("0.02")


def test_concurrent_approvals_only_one_external_call(framework, monkeypatch):
    barrier = threading.Barrier(2)

    def policy(vehicle):
        barrier.wait(timeout=10)
        return True

    session = CarImportTools(approval_policy=policy)
    tool = wrapped(session, framework)["paid_full"]
    calls = successful_payment(monkeypatch)

    def call():
        try:
            return invoke(tool, framework, CAR)["mode"]
        except PermissionError:
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(call) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(results) == ["LIVE_FULL", "denied"]
    assert len(calls) == 1
    assert session.reserved_calls == 1


def test_async_handlers_offload_http_to_worker(framework, monkeypatch):
    loop_thread = threading.get_ident()
    called = []

    def compare(**kwargs):
        called.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return {"worker": True}

    monkeypatch.setattr(adapter, "compare_car", compare)
    tool = wrapped(CarImportTools(), framework)["preview"]
    assert asyncio.run(ainvoke(tool, framework, CAR)) == {"worker": True}
    assert len(called) == 1


def test_cumulative_budget_with_multiple_calls(monkeypatch):
    session = CarImportTools(approval_policy=lambda vehicle: True,
                             max_usdc="0.007", max_paid_calls=9)
    calls = successful_payment(monkeypatch)
    session.paid_full(**CAR)
    session.paid_full(**CAR)
    with pytest.raises(PermissionError, match="budget exhausted"):
        session.paid_full(**CAR)
    assert len(calls) == 2
    assert session.reserved_usdc == Decimal("0.014")


def test_decimal_rounding_cannot_erase_reserved_budget(monkeypatch):
    session = CarImportTools(
        approval_policy=lambda vehicle: True,
        max_usdc="0.0066666666666666666666666666666666666666666666666667",
        max_paid_calls=3,
    )
    calls = successful_payment(monkeypatch)
    session.paid_full(**CAR)
    session.paid_full(**CAR)
    with pytest.raises(PermissionError, match="budget exhausted"):
        session.paid_full(**CAR)
    assert len(calls) == 2
    assert session.reserved_usdc == Decimal(
        "0.0133333333333333333333333333333333333333333333333334"
    )


def test_cancellation_keeps_reservation(framework, monkeypatch):
    started, release = threading.Event(), threading.Event()
    session = CarImportTools(approval_policy=lambda vehicle: True)

    def payment(args):
        started.set()
        assert release.wait(timeout=10)

    calls = successful_payment(monkeypatch, inspect=payment)
    tool = wrapped(session, framework)["paid_full"]

    async def cancel_in_flight():
        task = asyncio.create_task(ainvoke(tool, framework, CAR))
        try:
            assert await asyncio.to_thread(started.wait, 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert session.reserved_usdc == Decimal("0.02")
        finally:
            release.set()

    asyncio.run(cancel_in_flight())
    with pytest.raises(PermissionError, match="budget exhausted"):
        session.paid_full(**CAR)
    assert len(calls) == 1


def test_reentrant_policy_is_denied_without_deadlock(monkeypatch):
    def policy(vehicle):
        assert session.reserved_usdc == 0
        with pytest.raises(PermissionError, match="Reentrant"):
            session.paid_full(**CAR)
        return True

    session = CarImportTools(approval_policy=policy)
    calls = successful_payment(monkeypatch)
    session.paid_full(**CAR)
    assert len(calls) == 1


def test_policy_exception_and_mutation_denied_without_reservation():
    def policy(vehicle):
        vehicle["year"] = 2024
        return True

    session = CarImportTools(approval_policy=policy)
    with pytest.raises(TypeError):
        session.paid_full(**CAR)
    assert session.reserved_calls == 0


def test_async_policy_rejected_without_reservation():
    async def policy(vehicle):
        return True

    class AsyncPolicy:
        async def __call__(self, vehicle):
            return True

    for callback in (policy, AsyncPolicy(), "approve"):
        with pytest.raises(TypeError, match="synchronous"):
            CarImportTools(approval_policy=callback)
    session = CarImportTools(approval_policy=lambda vehicle: policy(vehicle))
    with pytest.raises(TypeError, match="awaitable"):
        session.paid_full(**CAR)
    assert session.reserved_calls == 0


@pytest.mark.parametrize("config", [
    {"max_usdc": "0.021"}, {"max_total_usdc": "0.03"}, {"max_usdc": "NaN"},
    {"max_usdc": 0}, {"max_paid_calls": 0}, {"max_paid_calls": True},
    {"max_usdc": "0.02", "max_total_usdc": "0.01"},
])
def test_invalid_operator_budget(config):
    with pytest.raises(ValueError):
        CarImportTools(**config)


def test_direct_paid_requires_operator():
    session = CarImportTools()
    with pytest.raises(PermissionError, match="operator"):
        session.paid_full(**CAR)
    assert session.reserved_calls == 0


@pytest.mark.parametrize("module,extra", [("langchain_core", "langchain"), ("agents", "openai")])
def test_missing_extra_has_install_hint_without_masking_dependency_bugs(monkeypatch, module, extra):
    original = adapter.importlib.import_module

    def missing(name):
        if name == module:
            raise ModuleNotFoundError("Missing SDK", name=module)
        return original(name)

    monkeypatch.setattr(adapter.importlib, "import_module", missing)
    with pytest.raises(ImportError, match=rf"\[{extra}\]"):
        getattr(CarImportTools(), f"as_{extra}_tools")()

    def broken(name):
        raise ModuleNotFoundError("Transitive SDK defect", name="transitive_sdk_dependency")

    monkeypatch.setattr(adapter.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError, match="Transitive SDK defect"):
        getattr(CarImportTools(), f"as_{extra}_tools")()


def test_fresh_base_import_and_demo_do_not_import_optional_sdks():
    code = """
import builtins, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'agents', 'langchain_core', 'openai', 'eth_account', 'x402'}:
        raise AssertionError('Unexpected dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from irish_car_import_assistant import CarImportTools
assert CarImportTools().offline_demo()['mode'] == 'SYNTHETIC_OFFLINE_DEMO'
assert 'agents' not in sys.modules and 'langchain_core' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
