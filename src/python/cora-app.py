"""Cora's shared agent factory and CLI examples. See the root README for setup."""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from agent_framework import Agent, MCPStdioTool, ToolTypes
from agent_framework.openai import OpenAIChatClient
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

# Microsoft Foundry Agent Configuration
ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
MODEL_DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")

AGENT_NAME = "mcp-agent"
AGENT_INSTRUCTIONS = "You are Cora, an intelligent and friendly AI assistant for Zava, a home improvement brand. You help customers with their DIY projects by understanding their needs and recommending the most suitable products from Zava’s catalog.​\n\nYour role is to:​\n\n- Engage with the customer in natural conversation to understand their DIY goals.​\n\n- Ask thoughtful questions to gather relevant project details.​\n\n- Be brief in your responses.​\n\n- Provide the best solution for the customer's problem and only recommend a relevant product within Zava's product catalog.​\n\n- Search Zava’s product database to identify 1 product that best match the customer’s needs.​\n\n- Clearly explain what each recommended Zava product is, why it’s a good fit, and how it helps with their project.​\n​\nYour personality is:​\n\n- Warm and welcoming, like a helpful store associate​\n\n- Professional and knowledgeable, like a seasoned DIY expert​\n\n- Curious and conversational—never assume, always clarify​\n\n- Transparent and honest—if something isn’t available, offer support anyway​\n\nIf no matching products are found in Zava’s catalog, say:​\n“Thanks for sharing those details! I’ve searched our catalog, but it looks like we don’t currently have a product that fits your exact needs. If you'd like, I can suggest some alternatives or help you adjust your project requirements to see if something similar might work.”"

# User inputs for the conversation
USER_INPUTS = [
    "Here’s a photo of my living room. Based on the lighting and layout, recommend a Zava eggshell paint.",
    "How much is Zava's eggshell paint?",
    "What are the current inventory levels for Zava's eggshell paint?",
]

def create_mcp_tools() -> list[ToolTypes]:
    return [
        MCPStdioTool(
            name="zava-customer-sales-stdio".replace("-", "_"),
            description="MCP server for zava-customer-sales-stdio",
            load_prompts=False,
            command=sys.executable,
            args=[
                str(Path(__file__).resolve().parent / "mcp_server/customer_sales/customer_sales.py"),
                "--stdio",
                "--RLS_USER_ID=00000000-0000-0000-0000-000000000000",
            ]
        ),
    ]

@asynccontextmanager
async def create_agent():
    """Share the exported agent configuration between the CLI and browser UI."""
    if not ENDPOINT or "YOUR-RESOURCE" in ENDPOINT:
        raise ValueError("Set AZURE_OPENAI_ENDPOINT in the root .env file. See README.md.")
    async with DefaultAzureCredential() as credential, Agent(
        client=OpenAIChatClient(
            azure_endpoint=ENDPOINT,
            model=MODEL_DEPLOYMENT_NAME,
            # For authentication, DefaultAzureCredential supports multiple authentication methods. Run `az login` in terminal for Azure CLI auth.
            credential=credential,
        ),
        name=AGENT_NAME,
        instructions=AGENT_INSTRUCTIONS,
        tools=create_mcp_tools(),
        default_options={
        },
    ) as agent:
        yield agent


async def main() -> None:
    async with create_agent() as agent:
        # Process user messages
        for user_input in USER_INPUTS:
            print(f"\n# User: '{user_input}'")
            printed_tool_calls = set()
            async for chunk in agent.run([user_input], stream=True):
                # log tool calls if any
                function_calls = [
                    c for c in chunk.contents 
                    if c.type == "function_call"
                ]
                for call in function_calls:
                    if call.call_id not in printed_tool_calls:
                        print(f"Tool calls: {call.name}")
                        printed_tool_calls.add(call.call_id)
                if chunk.text:
                    print(chunk.text, end="")
            print("")
        
        print("\n--- All tasks completed successfully ---")

    # Give additional time for all async cleanup to complete
    await asyncio.sleep(1.0)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProgram interrupted by user")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("Program finished.")
