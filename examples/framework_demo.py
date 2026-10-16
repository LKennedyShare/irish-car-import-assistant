"""Invoke real framework tools locally, offline by default; never run a model."""

import argparse
import asyncio
import json

from irish_car_import_assistant import CarImportTools


CAR = {
    "make": "Volkswagen", "model": "Golf", "year": 2023,
    "mileage_km": 50000, "fuel_type": "Petrol", "co2_g_km": 125,
    "transmission": None, "origin": "ANY",
}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework", choices=("langchain", "openai"), required=True)
    parser.add_argument("--live", action="store_true",
                        help="Explicitly request a free live preview (consumes quota)")
    args = parser.parse_args()
    session = CarImportTools()
    name, inputs = ("preview", CAR) if args.live else ("offline_demo", {})
    if args.framework == "langchain":
        tools = {tool.name: tool for tool in session.as_langchain_tools()}
        result = await tools[name].ainvoke(inputs)
    else:
        from agents.tool_context import ToolContext

        tools = {tool.name: tool for tool in session.as_openai_tools()}
        arguments = json.dumps(inputs)
        context = ToolContext(context=None, tool_name=name,
                              tool_call_id="local-demo", tool_arguments=arguments)
        result = await tools[name].on_invoke_tool(context, arguments)
    print(f"{args.framework}: {result['mode']}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
