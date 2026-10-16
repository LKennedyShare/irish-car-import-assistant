Irish Car Import Assistant
==========================

Is importing a used car from the UK or Northern Ireland cheaper than buying in
Ireland? This client compares the Irish asking-price median with estimated
landed import costs and explains the result.

* Free preview first; no wallet or LLM key needed.
* Guided CLI, structured flags, JSON output and a reusable Python function.
* Optional real LangChain and OpenAI Agents SDK tools, offline-first.
* Optional full results use explicitly authorized x402 payments, capped at
  **0.02 USDC per invocation**.
* No car purchases, import execution, backend code, databases or credentials.

It calls the public `UK Ireland Used Car Import Agent
<https://irish-auto-arbitrage-production.up.railway.app/>`_. This is a
deterministic buyer integration, not an autonomous LLM or an A2A-protocol server.

Install and try offline
-----------------------

Requires Python 3.12 or later. From a fresh terminal:

.. code-block:: powershell

   python -m pip install irish-car-import-assistant
   car-import-assistant --demo

The demo is labelled ``SYNTHETIC_OFFLINE_DEMO``. Its figures and comparable
counts are illustrative, not live findings. It uses no network, wallet or
payment signing. ``python -m irish_car_import_assistant`` accepts the same
arguments as the console command.

Run a free comparison
---------------------

For a guided free comparison:

.. code-block:: powershell

   car-import-assistant --interactive

Enter the actual car details and CO2 figure, then type ``compare`` to authorize
a free request. Do not guess CO2 or the underlying fuel of a hybrid.

Or provide structured input:

.. code-block:: powershell

   car-import-assistant --make Volkswagen --model Golf --year 2023 --mileage-km 50000 --fuel-type Petrol --co2-g-km 125

Add ``--json`` for one machine-readable result, ``--origin GB`` or ``--origin NI``
to restrict the route, or ``--transmission Automatic``. The default origin is
``ANY``. Coverage changes; this example is not a promise of matching live stock.

Supported fuels: Petrol, Diesel, Electric, Petrol Hybrid, Diesel Hybrid,
Petrol Plug-in Hybrid and Diesel Plug-in Hybrid.

The service's default preview allowance is **3 attempts per UTC day per IP**,
shared with its REST and MCP callers. No-data and invalid attempts may consume
the allowance. A quota failure never triggers a paid upgrade. HTTP 429 errors
preserve ``Retry-After``; errors go to stderr with a nonzero exit.

The output explains the Irish median and comparable count, route totals,
savings or shortfall, transport, customs, VAT, VRT, NOx and other costs.
Individual listing links are reserved for the full paid result.

Inspect or explicitly pay for full results
------------------------------------------

Repeat a structured command with ``--full`` to inspect its quote without paying.
An ordinary MCP connection does not supply a wallet or payment capability.

For a paid result, set ``X402_CLIENT_PRIVATE_KEY`` in your local shell to a
throwaway wallet funded with only a small USDC balance on the quoted network,
then explicitly authorize:

.. code-block:: powershell

   car-import-assistant --make Volkswagen --model Golf --year 2023 --mileage-km 50000 --fuel-type Petrol --co2-g-km 125 --full --pay --max-usdc 0.02

Never put the private key in source code, request JSON, prompts, logs or this
repository. Base mainnet uses real USDC.

Only ``--full --pay`` authorizes one capped paid retry. There are no automatic
payment retries, and redirects are refused. Only exact USDC on Base and Base
Sepolia is accepted. A missing settlement response is explicitly unconfirmed,
not proof that payment succeeded. Each invocation has its own cap; this is not
a daily wallet spending limit.

No command buys a car, pays an invoice, executes an import or follows listing
links. The result is advisory, not tax, customs or financial advice. Asking
prices are not sale prices. Independently confirm vehicle history, bank
details, FX, duty/VAT eligibility, Revenue OMSP/VRT and NOx before buying.

Use from another agent
----------------------

.. code-block:: python

   from irish_car_import_assistant import compare_car

   result = compare_car(
       make="Volkswagen", model="Golf", year=2023,
       mileage_km=50000, fuel_type="Petrol", co2_g_km=125,
   )
   print(result["verdict"], result["routes"])

This defaults to a free preview. ``full=True`` inspects a quote; payment also
requires ``pay=True`` and accepts ``max_usdc="0.02"``. Errors are surfaced as
``ValueError`` or ``httpx.HTTPError`` rather than invented comparisons.

``--base-url`` can target another compatible HTTPS service; HTTP is allowed
only on localhost/loopback. Requests identify as ``car-import-assistant``.
That label is not evidence that a caller is a genuine independent customer.

Development
-----------

.. code-block:: powershell

   git clone https://github.com/LKennedyShare/irish-car-import-assistant.git
   cd irish-car-import-assistant
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -e ".[test,langchain,openai]"
   python -m pytest -q

Tests mock HTTP and payments. Test/release jobs run tests and the offline
demo only; they do not use a wallet or make paid requests.

Release and unpaid discovery automation
---------------------------------------

Version 0.2.0 adds optional framework adapters. Trusted publishing is configured for this
repository without stored API tokens. The publisher identity is:

* PyPI project: ``irish-car-import-assistant``
* GitHub owner: ``LKennedyShare``
* Repository: ``irish-car-import-assistant``
* Workflow filename: ``publish.yml``
* Environment: ``pypi`` (exact, fixed GitHub environment name)

The GitHub environment is configured. No API token or password
is used. No manual approval per release is required by default; environment
protection rules can impose approval if the owner chooses.
Publishing a GitHub release with a matching version tag builds that
version's wheel and sdist, runs mocked tests, the installed offline CLI and
strict Twine checks, then publishes via OIDC. Only ``release.published`` triggers
publication, not a PR or manual workflow run. Tags must exactly match
``v<pyproject.version>`` and package identity is checked before build.
PyPI versions are immutable: future releases need a new version and matching
tag. Duplicate uploads fail rather than silently skipping existing files.
The initial published version is ``0.1.1``; do not reuse that release tag.
Release ``0.2.0`` must use ``v0.2.0``; published versions and tags must not be
overwritten or reused.

``discovery.yml`` runs nightly at 03:23 UTC and supports manual dispatch.
It uses Python's standard library only, no SDK, wallet, secrets or funds.
An independent job installs the published PyPI package and runs only its offline
synthetic demo, outside a repository checkout; it never invokes a paid command.
It reads public health/OpenAPI/cards/homepage/buyers metadata and sends one
unsigned paid REST quote probe and one empty-arguments MCP quote probe.
It never calls the free preview, signs, pays, changes listings or follows
response URLs. The car service uses REST/MCP, **not the A2A protocol**.
All requests identify as ``scout-distribution-monitor``: diagnostics, not
customers, purchases, demand or independent adoption.

Run locally with ``python scripts/check_discovery.py --service car --output discovery.json``.
The JSON, Actions artifact and step summary contain only fixed check names and
statuses, never response bodies, headers or private data. Requests refuse
redirects, cap bodies at 2 MB and use 15-second socket timeouts; failures exit
nonzero. A missing PyPI project is explicitly ``pending``, without masking
service failures. The scheduled workflow uses ``--require-pypi`` to enforce
PyPI availability and repository/MIT/name metadata.
The homepage and ``/buyers.txt`` must link this public client repository.

GitHub schedules can be delayed or dropped and public-repository schedules
may be disabled after 60 days without repository activity. This is not
guaranteed continuous uptime monitoring; review Actions and re-enable schedules
when needed. No artificial activity, stars or promotional posting is generated.

Licensing
---------

MIT licensed; see ``LICENSE``. This covers this buyer client's source code,
not the separate service, its data or its API usage terms.

Framework integration (0.2.0)
-----------------------------

Install only the adapter your application needs, or both:

.. code-block:: powershell

   python -m pip install "irish-car-import-assistant[langchain,openai]"

The extras use ``langchain-core>=1.6,<2`` and ``openai-agents>=0.24,<1``.
Base imports and offline demos do not import either SDK. Missing extras produce
an installation hint; broken transitive SDK imports are not hidden.

``CarImportTools`` exposes ``offline_demo()``, ``preview(...)`` and
``quote(...)``. Vehicle arguments are ``make``, ``model``, ``year``,
``mileage_km``, ``fuel_type``, ``co2_g_km``, optional ``transmission=None`` and
``origin="ANY"``. They use the same validation and normalization as
``compare_car``. Results are JSON-serializable dictionaries; errors propagate,
not fabricated comparisons. A preview consumes the service's shared free quota.
A quote calls the full endpoint with payment disabled, even if a wallet exists.

``as_langchain_tools()`` creates real ``StructuredTool.from_function`` tools
with strict Pydantic argument schemas and synchronous/asynchronous handlers.
``as_openai_tools()`` creates real ``agents.function_tool`` tools with strict
schemas and ``failure_error_function=None``. Both reject unexpected arguments.
Async handlers offload synchronous HTTP work via ``asyncio.to_thread``.
Factory construction makes no model, HTTP, payment or tracing calls. This module
does not record PII; configure the host application's SDK tracing/logging and
retention separately, and do not include personal information in car fields.

Run the checked-in local examples from a source checkout:

.. code-block:: powershell

   python examples/framework_demo.py --framework langchain
   python examples/framework_demo.py --framework openai

Both invoke the actual framework tool locally and print labelled
``SYNTHETIC_OFFLINE_DEMO`` output. Neither needs a model-provider key, wallet or
network. Only an explicit ``--live`` invokes the free preview and consumes
quota. The examples never invoke a model or request payment.

For local invocation in an existing application:

.. code-block:: python

   from irish_car_import_assistant import CarImportTools

   session = CarImportTools()  # Retain this instance for the entire session.
   tools = {tool.name: tool for tool in session.as_langchain_tools()}
   synthetic = tools["offline_demo"].invoke({})

An OpenAI SDK tool can also be invoked locally without ``Agent`` or ``Runner``:

.. code-block:: python

   import asyncio
   from agents.tool_context import ToolContext
   from irish_car_import_assistant import CarImportTools

   session = CarImportTools()
   demo = session.as_openai_tools()[0]
   context = ToolContext(
       context=None, tool_name=demo.name,
       tool_call_id="local-demo", tool_arguments="{}",
   )
   synthetic = asyncio.run(demo.on_invoke_tool(context, "{}"))

Actual agent construction belongs to the host application. LangChain's
``create_agent`` additionally requires the application's ``langchain`` and
chosen provider packages; the adapter extra installs only ``langchain-core``:

.. code-block:: python

   from langchain.agents import create_agent
   from irish_car_import_assistant import CarImportTools

   session = CarImportTools()

   def build_langchain_agent(configured_chat_model):
       return create_agent(
           model=configured_chat_model,
           tools=session.as_langchain_tools(),
           system_prompt="Compare car import costs. Do not invent vehicle CO2.",
       )

.. code-block:: python

   from agents import Agent, RunConfig
   from irish_car_import_assistant import CarImportTools

   session = CarImportTools()

   def build_openai_agent(model_id):
       return Agent(
           name="Irish car import adviser",
           model=model_id,
           instructions="Use the car tools. Never invent vehicle details.",
           tools=session.as_openai_tools(),
       )

   run_config = RunConfig(tracing_disabled=True)
   # The host may later call Runner with its configured model and run_config.
   # No Agent/Runner or model invocation is performed at import time here.

Model execution is separate, needs the host's provider configuration and may
incur provider charges. Tool factories do not grant automatic discovery,
installation, distribution or guaranteed reach to other agents.

Operator-only paid policy
~~~~~~~~~~~~~~~~~~~~~~~~~

By default neither adapter exposes ``paid_full``. The model never receives
payment switches, wallet/key fields, caps, approval statements or a base URL as
tool arguments. A base URL is trusted operator constructor configuration only.
To add ``paid_full``, supply a **synchronous operator-owned callable** as
``approval_policy``. It sees an immutable copy of validated, normalized vehicle
parameters, not a model's assertion of consent, and must return literal
``True``. Truthy values, awaitables, exceptions and denied requests do not
reserve budget or make network calls.

For autonomous, non-human approval, explicitly allowlist an exact request in
operator code (not prompts, model output or untrusted listing content):

.. code-block:: python

   from irish_car_import_assistant import CarImportTools

   ALLOWED = frozenset({
       tuple(sorted({
           "make": "Volkswagen", "model": "Golf", "year": 2023,
           "mileage_km": 50000, "fuel_type": "Petrol", "co2_g_km": 125,
           "origin": "GB",
       }.items())),
   })

   def approved_vehicle(vehicle):
       return tuple(sorted(vehicle.items())) in ALLOWED

   paid_session = CarImportTools(
       approval_policy=approved_vehicle,
       max_usdc="0.02", max_total_usdc="0.02", max_paid_calls=1,
   )
   paid_tools = paid_session.as_openai_tools()  # Or as_langchain_tools().
   # Only a later paid_full invocation matching ALLOWED can reach compare_car.

Normalization trims make/model text (preserving case), canonicalizes fuel,
uppercases origin and title-cases transmission. Unspecified transmission is
omitted from the policy mapping. This allowlist therefore approves GB only,
not ANY/NI or a request with an additional transmission constraint. It does not
approve a purchase or import. Keep ``X402_CLIENT_PRIVATE_KEY`` only in the
operator's wallet environment, never the model context.

One retained object defaults to **one paid reservation**, capped at
**0.02 USDC total service fees**. Both per-call ``max_usdc`` and cumulative
``max_total_usdc`` must be positive and at most 0.02. ``max_paid_calls`` can be
raised only within that cumulative cap. The entire per-call cap is reserved
under a thread lock before calling the existing ``compare_car`` payment path;
concurrent calls cannot double-reserve. Policies run outside the budget lock;
reentrant paid approval is rejected. No retry loop or duplicate payment/signing
implementation is added.

Reservations consume both the call allowance and full cap even on failure,
timeout, cancellation or unknown settlement, with no refunds or automatic
retries. An unconfirmed settlement is not success. Read-only
``reserved_calls`` and ``reserved_usdc`` expose current accounting.
The budget is **per-object, in-memory only**, not durable, daily or account-wide.
Recreating an object resets it: **never recreate it per tool call**. Production
operators need a durable, atomic policy/budget across sessions, processes and
restarts; these local limits are not a substitute. Model-provider charges,
wallet funding and other external costs are outside this service-fee cap.
