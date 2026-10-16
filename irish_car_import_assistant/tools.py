"""Optional framework adapters with operator-only, session-bounded paid access."""

import asyncio
import importlib
import inspect
from collections.abc import Callable, Mapping
from decimal import Decimal, localcontext
from functools import wraps
from threading import Lock, local
from types import MappingProxyType

from .assistant import BASE_URL, _vehicle, compare_car, spend_cap


def _sdk(module, extra):
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name != module:
            raise
        raise ImportError(
            f"Install the optional adapter: pip install 'irish-car-import-assistant[{extra}]'"
        ) from exc


class CarImportTools:
    """Reuse one instance across an agent session; reservations never get refunded.

    approval_policy receives an immutable, normalized vehicle mapping and must
    synchronously return literal True. It is never called while holding the
    budget lock. No credentials, approval statements or caps are model arguments.
    """

    def __init__(
        self, *, base_url: str = BASE_URL,
        approval_policy: Callable[[Mapping], bool] | None = None,
        max_usdc: str = "0.02", max_total_usdc: str = "0.02",
        max_paid_calls: int = 1,
    ):
        if approval_policy is not None and (
            not callable(approval_policy)
            or inspect.iscoroutinefunction(approval_policy)
            or inspect.iscoroutinefunction(getattr(approval_policy, "__call__", None))
        ):
            raise TypeError("approval_policy must be a synchronous callable")
        if type(max_paid_calls) is not int or max_paid_calls < 1:
            raise ValueError("max_paid_calls must be a positive integer")
        self._cap = spend_cap(max_usdc)
        self._total_cap = spend_cap(max_total_usdc)
        if self._cap > self._total_cap:
            raise ValueError("max_usdc cannot exceed max_total_usdc")
        self._base_url = base_url
        self._policy = approval_policy
        self._max_paid_calls = max_paid_calls
        self._budget_precision = max(
            28, len(self._cap.as_tuple().digits) + len(str(max_paid_calls)) + 1,
        )
        self._lock = Lock()
        self._approving = local()
        self._reserved_usdc = Decimal("0")
        self._reserved_calls = 0

    @property
    def reserved_usdc(self) -> Decimal:
        with self._lock:
            return self._reserved_usdc

    @property
    def reserved_calls(self) -> int:
        with self._lock:
            return self._reserved_calls

    def offline_demo(self) -> dict:
        """Return labelled synthetic offline data, with no network or wallet access."""
        return compare_car(demo=True)

    def preview(
        self, make: str, model: str, year: int, mileage_km: int,
        fuel_type: str, co2_g_km: int, transmission: str | None = None,
        origin: str = "ANY",
    ) -> dict:
        """Request a live free preview; uses the service's shared preview quota."""
        vehicle = _vehicle(make, model, year, mileage_km, fuel_type, co2_g_km,
                           transmission, origin)
        return compare_car(**vehicle, base_url=self._base_url)

    def quote(
        self, make: str, model: str, year: int, mileage_km: int,
        fuel_type: str, co2_g_km: int, transmission: str | None = None,
        origin: str = "ANY",
    ) -> dict:
        """Inspect a full-result quote without authorizing or making a payment."""
        vehicle = _vehicle(make, model, year, mileage_km, fuel_type, co2_g_km,
                           transmission, origin)
        return compare_car(**vehicle, base_url=self._base_url, full=True, pay=False)

    def paid_full(
        self, make: str, model: str, year: int, mileage_km: int,
        fuel_type: str, co2_g_km: int, transmission: str | None = None,
        origin: str = "ANY",
    ) -> dict:
        """Request full results only under the operator's policy and reserved budget."""
        vehicle = _vehicle(make, model, year, mileage_km, fuel_type, co2_g_km,
                           transmission, origin)
        if self._policy is None:
            raise PermissionError("Paid access requires an operator approval_policy")
        if getattr(self._approving, "active", False):
            raise PermissionError("Reentrant paid approval is not allowed")
        self._approving.active = True
        try:
            approved = self._policy(MappingProxyType(dict(vehicle)))
            if inspect.isawaitable(approved):
                if inspect.iscoroutine(approved):
                    approved.close()
                raise TypeError("approval_policy must return synchronously, not an awaitable")
            if approved is not True:
                raise PermissionError("Operator policy denied this vehicle")
        finally:
            self._approving.active = False
        # Reserve the entire cap atomically before any external call. Failures,
        # cancellation and unknown settlement consume it; there are no retries.
        with self._lock, localcontext() as context:
            context.prec = self._budget_precision
            reserved = self._reserved_usdc + self._cap
            if (self._reserved_calls >= self._max_paid_calls
                    or reserved > self._total_cap):
                raise PermissionError("Session paid-call or USDC reservation budget exhausted")
            self._reserved_calls += 1
            self._reserved_usdc = reserved
        return compare_car(**vehicle, base_url=self._base_url, full=True, pay=True,
                           max_usdc=str(self._cap))

    def _functions(self):
        functions = [self.offline_demo, self.preview, self.quote]
        if self._policy is not None:
            functions.append(self.paid_full)
        return functions

    @staticmethod
    def _async(function):
        @wraps(function)
        async def invoke(*args, **kwargs):
            return await asyncio.to_thread(function, *args, **kwargs)
        return invoke

    @staticmethod
    def _schema(function):
        from pydantic import ConfigDict, create_model

        return create_model(
            f"{function.__name__}_arguments",
            __config__=ConfigDict(extra="forbid", strict=True),
            **{
                name: (parameter.annotation, ... if parameter.default
                       is inspect.Parameter.empty else parameter.default)
                for name, parameter in inspect.signature(function).parameters.items()
            },
        )

    def as_langchain_tools(self) -> list:
        """Build real StructuredTools; install the ``langchain`` extra first."""
        sdk = _sdk("langchain_core", "langchain")
        structured = importlib.import_module(f"{sdk.__name__}.tools").StructuredTool
        tools = []
        for function in self._functions():
            tools.append(structured.from_function(
                func=function, coroutine=self._async(function),
                args_schema=self._schema(function),
            ))
        return tools

    def as_openai_tools(self) -> list:
        """Build Agents SDK FunctionTools without models, keys or tracing calls."""
        sdk = _sdk("agents", "openai")
        from pydantic import StrictInt, StrictStr

        tools = []
        for function in self._functions():
            invoke = self._async(function)
            # The SDK derives its flat Pydantic schema from this signature.
            signature = inspect.signature(function)
            strict = {int: StrictInt, str: StrictStr, str | None: StrictStr | None}
            invoke.__signature__ = signature.replace(parameters=[
                parameter.replace(annotation=strict.get(parameter.annotation, parameter.annotation))
                for parameter in signature.parameters.values()
            ])
            invoke.__annotations__ = {
                name: strict.get(annotation, annotation)
                for name, annotation in function.__annotations__.items()
            }
            tool = sdk.function_tool(
                invoke, failure_error_function=None, strict_mode=True,
            )
            # SDK JSON-schema strictness is not necessarily runtime extra-field
            # rejection. Enforce the same strict Pydantic boundary locally too.
            tool.on_invoke_tool = self._validated_invoker(
                tool.on_invoke_tool, self._schema(function),
            )
            tools.append(tool)
        return tools

    @staticmethod
    def _validated_invoker(invoke, schema):
        async def validated(context, arguments):
            schema.model_validate_json(arguments)
            return await invoke(context, arguments)
        return validated
