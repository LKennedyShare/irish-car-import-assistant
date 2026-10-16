Irish Car Import Assistant
==========================

Is importing a used car from the UK or Northern Ireland cheaper than buying in
Ireland? This client compares the Irish asking-price median with estimated
landed import costs and explains the result.

* Free preview first; no wallet or LLM key needed.
* Guided CLI, structured flags, JSON output and a reusable Python function.
* Optional full results use explicitly authorized x402 payments, capped at
  **0.02 USDC per invocation**.
* No car purchases, import execution, backend code, databases or credentials.

It calls the public `UK Ireland Used Car Import Agent
<https://irish-auto-arbitrage-production.up.railway.app/>`_. This is a
deterministic buyer integration, not an autonomous LLM or an A2A-protocol server.

Install and try offline
----------------------

Requires Python 3.12 or later. From a fresh terminal:

.. code-block:: powershell

   python -m pip install "git+https://github.com/LKennedyShare/irish-car-import-assistant.git"
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
-----------------------------------------

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
   python -m pip install -e ".[test]"
   python -m pytest -q

Tests mock HTTP and payments. The GitHub workflow runs tests and the offline
demo only; it does not use a wallet or make paid requests.

Licensing
---------

MIT licensed; see ``LICENSE``. This covers this buyer client's source code,
not the separate service, its data or its API usage terms.
