# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import inspect
import json
import os
import uuid
from zoneinfo import ZoneInfo

import requests
from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager
from google import genai
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.code_executors import AgentEngineSandboxCodeExecutor
from google.adk.memory import VertexAiMemoryBankService
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.cloud import firestore, storage
from google.genai import types

from .a2ui_utils import a2ui_callback

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "YOUR_GCP_PROJECT_ID_HERE")
BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "YOUR_GCS_BUCKET_NAME_HERE")
db = firestore.Client(project=PROJECT_ID)
gcs_client = storage.Client(project=PROJECT_ID)
COLLECTION_NAME = "research_notes"

DEPLOYMENT_METADATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "deployment_metadata.json"
)
AGENT_ENGINE_ID = os.getenv("AGENT_ENGINE_RESOURCE_NAME", "projects/YOUR_PROJECT_NUMBER_HERE/locations/us-east1/reasoningEngines/YOUR_REASONING_ENGINE_ID_HERE")

if os.path.exists(DEPLOYMENT_METADATA_PATH):
    try:
        with open(DEPLOYMENT_METADATA_PATH, "r") as f:
            metadata = json.load(f)
            AGENT_ENGINE_ID = metadata.get("remote_agent_runtime_id", AGENT_ENGINE_ID)
    except Exception:
        pass

code_executor = AgentEngineSandboxCodeExecutor(
    agent_engine_resource_name=AGENT_ENGINE_ID
)


def fetch_live_stock_quote(ticker: str) -> dict:
    """Fetches real-time stock quote and market metrics for a company by ticker symbol.

    Args:
        ticker: The stock ticker symbol (e.g. 'AAPL', 'GOOGL', 'NVDA', 'TSLA').

    Returns:
        A dictionary containing live market price, currency, previous close, and 52-week range.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.strip().upper()}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            meta = resp.json()["chart"]["result"][0]["meta"]
            return {
                "ticker": meta.get("symbol"),
                "currency": meta.get("currency"),
                "current_price": meta.get("regularMarketPrice"),
                "previous_close": meta.get("chartPreviousClose"),
                "52_week_high": meta.get("fiftyTwoWeekHigh"),
                "52_week_low": meta.get("fiftyTwoWeekLow"),
            }
        return {
            "error": f"Failed to fetch quote for ticker '{ticker}' (HTTP {resp.status_code})."
        }
    except Exception as e:
        return {"error": f"Error fetching stock quote for '{ticker}': {str(e)}"}


def get_company_info(ticker: str) -> dict:
    """Fetches company profile information including full company name, sector, industry, and exchange.

    Args:
        ticker: The stock ticker symbol (e.g. 'AAPL', 'NVDA', 'GOOGL', 'TSLA').

    Returns:
        A dictionary containing company profile details.
    """
    url = f"https://query1.finance.yahoo.com/v1/finance/search?q={ticker.strip().upper()}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            quotes = resp.json().get("quotes", [])
            if quotes:
                q = quotes[0]
                return {
                    "ticker": q.get("symbol"),
                    "company_name": q.get("shortname") or q.get("longname"),
                    "sector": q.get("sector", "N/A"),
                    "industry": q.get("industry", "N/A"),
                    "exchange": q.get("exchDisp", "N/A"),
                    "type": q.get("quoteType", "EQUITY"),
                }
    except Exception as e:
        pass
    return {
        "ticker": ticker.strip().upper(),
        "info": f"Company profile information for {ticker.strip().upper()}",
    }


def create_research_note(
    company: str,
    ticker: str,
    sector: str,
    research_summary: str,
    analyst_rating: str,
) -> dict:
    """Creates a new research note for a company and stores it in Firestore.

    Args:
        company: The name of the company (e.g. 'NVIDIA Corporation').
        ticker: The stock ticker symbol (e.g. 'NVDA').
        sector: The industry sector (e.g. 'Semiconductors').
        research_summary: Detailed summary of research analysis.
        analyst_rating: Analyst rating (e.g. 'Buy', 'Hold', 'Sell', 'Strong Buy').

    Returns:
        A dictionary containing the created research note details.
    """
    ticker_clean = ticker.strip().upper()
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    note_data = {
        "company": company.strip(),
        "ticker": ticker_clean,
        "sector": sector.strip(),
        "research_summary": research_summary.strip(),
        "analyst_rating": analyst_rating.strip(),
        "created_at": created_at,
    }
    db.collection(COLLECTION_NAME).document(ticker_clean).set(note_data)
    return note_data


def get_research_note(ticker: str) -> dict:
    """Retrieves a research note from Firestore by company stock ticker symbol.

    Args:
        ticker: The stock ticker symbol to look up (e.g. 'AAPL', 'TSLA', 'GOOGL').

    Returns:
        A dictionary containing the research note if found, or an error message dict.
    """
    ticker_clean = ticker.strip().upper()
    doc_ref = db.collection(COLLECTION_NAME).document(ticker_clean)
    doc = doc_ref.get()
    if doc.exists:
        return doc.to_dict()

    query_docs = (
        db.collection(COLLECTION_NAME).where("ticker", "==", ticker_clean).get()
    )
    if query_docs:
        return query_docs[0].to_dict()

    return {"error": f"No research note found for ticker symbol '{ticker_clean}'."}


def list_research_notes() -> list:
    """Lists all stored research notes from Firestore.

    Returns:
        A list of dictionaries, each representing a research note.
    """
    docs = db.collection(COLLECTION_NAME).stream()
    notes = [doc.to_dict() for doc in docs]
    return notes


async def generate_dashboard_image(
    prompt_details: str = "Revenue comparison, KPI scorecards, stakeholder insights, peer comparison results, and competitive positioning",
    tool_context: ToolContext = None,
) -> dict:
    """Generates a Financial Research Studio dashboard image using gemini-3.1-flash-lite-image, saves it as an artifact, uploads it to Cloud Storage, and returns the public URL.

    Args:
        prompt_details: Metrics and details for the dashboard visualization (revenue comparison, KPI scorecards, stakeholder insights, peer comparison, competitive positioning).

    Returns:
        A dictionary containing the public Cloud Storage URL and status.
    """
    prompt = (
        "Create a professional, dark-mode Financial Research Studio executive dashboard image "
        "supporting revenue comparison charts, KPI scorecards, stakeholder insights, "
        "peer comparison results, and competitive positioning matrix. "
        f"Details: {prompt_details}"
    )

    img_bytes = None
    mime_type = "image/jpeg"

    for loc, model_name in [
        ("global", "gemini-3.1-flash-lite-image"),
        ("us-central1", "gemini-2.5-flash-image"),
    ]:
        try:
            client = genai.Client(vertexai=True, project=PROJECT_ID, location=loc)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
            )
            for p in response.candidates[0].content.parts:
                if p.inline_data:
                    img_bytes = p.inline_data.data
                    mime_type = p.inline_data.mime_type or "image/jpeg"
                    break
            if img_bytes:
                break
        except Exception:
            continue

    if not img_bytes:
        return {"error": "Failed to generate dashboard image."}

    filename = f"dashboard_{uuid.uuid4().hex[:8]}.jpg"

    if tool_context and hasattr(tool_context, "save_artifact"):
        try:
            artifact_part = types.Part.from_bytes(data=img_bytes, mime_type=mime_type)
            res = tool_context.save_artifact(filename=filename, artifact=artifact_part)
            if inspect.isawaitable(res):
                await res
        except Exception:
            pass

    bucket = gcs_client.bucket(BUCKET_NAME)
    blob = bucket.blob(filename)
    blob.upload_from_string(img_bytes, content_type=mime_type)

    public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{filename}"
    return {
        "status": "success",
        "filename": filename,
        "public_url": public_url,
        "message": "Dashboard image generated and published to Cloud Storage successfully.",
    }


def get_weather(query: str) -> str:
    """Simulates a web search. Use it get information on weather.

    Args:
        query: A string containing the location to get weather information for.

    Returns:
        A string with the simulated weather information for the queried location.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        return "It's 60 degrees and foggy."
    return "It's 90 degrees and sunny."


def get_current_time(query: str) -> str:
    """Simulates getting the current time for a city.

    Args:
        query: The name of the city to get the current time for.

    Returns:
        A string with the current time information.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        tz_identifier = "America/Los_Angeles"
    else:
        return f"Sorry, I don't have timezone information for query: {query}."

    tz = ZoneInfo(tz_identifier)
    now = datetime.datetime.now(tz)
    return f"The current time for query {query} is {now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}"


async def generate_memories_callback(callback_context: CallbackContext):
    """Write: after each turn, send the session events to Memory Bank for durable fact extraction."""
    try:
        await callback_context.add_session_to_memory()
    except Exception as e:
        print(f"Memory bank callback skipped: {e}")
    return None


MEMORY_BANK_ID = AGENT_ENGINE_ID.split("/")[-1]


def memory_bank_service_builder():
    """Builds VertexAiMemoryBankService pointed at the Agent Engine Memory Bank instance."""
    return VertexAiMemoryBankService(
        project=PROJECT_ID,
        location="us-east1",
        agent_engine_id=MEMORY_BANK_ID,
    )


schema_manager = A2uiSchemaManager(
    version="0.8",
    catalogs=[BasicCatalog.get_config("0.8")],
)

a2ui_instruction = schema_manager.generate_system_prompt(
    role_description="Advanced Financial Research Studio AI assistant with cross-session memory and Python code execution capabilities.",
    workflow_description="""Analyze financial research requests, execute sandbox Python code for calculations, query live quotes/company info/notes, generate dashboard images, and return structured A2UI UI surfaces when appropriate.

CRITICAL WELCOME EXPERIENCE RULES:
1. When starting a new session or receiving a greeting ("hello", "hi", "welcome", "show welcome screen"):
   - AUTOMATICALLY call `list_research_notes` to discover tracked companies from Firestore research notes and preloaded memory.
   - ALWAYS render the response using an A2UI Card component with the following EXACT structure and text:
     * Header: "Welcome to Financial Research Studio." (usageHint: "h2")
     * Section 1 Title: "Current Watchlist:" (usageHint: "h3")
     * Section 1 Content: List of tracked companies from notes/memory (e.g. "• NVDA", "• TSLA"), OR if none found: "No active watchlist found. Ask me to create one." (usageHint: "body")
     * Section 2 Title: "Available Workflows:" (usageHint: "h3")
     * Section 2 Content (structured list):
       • Live Stock Analysis
       • Research Notes
       • Peer Comparison
       • Competitive Positioning
       • Dashboard Generation
     * Closing Prompt: "What would you like to analyze today?" (usageHint: "h4")

CRITICAL AUTOMATIC DATA FETCHING & WORKFLOW RULES:
1. NEVER ask the user to input or provide financial numbers, revenue metrics, margins, or valuation ratios. The user only needs to supply company names or ticker symbols (e.g. AAPL vs MSFT).
2. When a peer comparison is requested, AUTOMATICALLY:
   - Call `get_company_info` and `fetch_live_stock_quote` for every ticker requested.
   - Automatically assemble the financial dataset and pass it into the Python Sandbox Code Executor.
   - Run sandbox Python calculations to compute Revenue Growth, Net Margin, Operating Margin, ROE, P/E Ratio, and Earnings Growth.
   - Call `generate_dashboard_image` to create the comparison dashboard card.
   - Derive KPI rankings (Revenue Growth Ranking, Net Margin Ranking, Operating Margin Ranking, ROE Ranking, P/E Ranking) and Competitive Positioning tiers (Leader, Challenger, Specialist).
3. NEVER output intermediate reasoning, chain-of-thought, code planning, or sandbox preparation text to the user.
4. Render all final peer comparison rankings, positioning analysis, executive summary, and dashboard images through A2UI KPI cards and comparison tables.
5. Write sandbox calculation code as executable Python code blocks directly.""",
    ui_description=(
        "Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows. "
        "Never nest a Card inside a Card. "
        "Use ONLY these components: Card, Column, Row, Text, and Image. Do not use "
        "Table or Heading (unsupported), or Buttons, actions, or forms (they do "
        "nothing in adk web). "
        "Do NOT output any prose, internal reasoning, or chain-of-thought before or after the A2UI JSON array. "
        "For multi-company peer comparisons, format rankings (Revenue Growth Ranking, Net Margin Ranking, Operating Margin Ranking, ROE Ranking, P/E Ranking), Competitive Positioning Analysis (Leader, Challenger, Specialist), Executive Summary, and Dashboard Card/Image output into clean Rows and Columns of Text and Image components within A2UI Cards using sandbox-generated metrics. "
        "You may include one Image component, but only when you have a public https "
        "URL for the image (for example the URL an image tool returns after uploading "
        "to a public bucket). Set the Image url to that exact https link, for example "
        '{"Image": {"url": {"literalString": "https://..."}}}. Never point an '
        "Image at a bare filename, an artifact name, or a non-http(s) path. If you do "
        "not have a public URL, add a short Text line noting the image instead. "
        "No markdown in text; use the usageHint property ('h1', 'h2', 'body') for "
        "headings and emphasis. "
        "Output ONLY the raw A2UI JSON array — no prose, and never wrap it in "
        "<a2a_datapart_json> tags or 'kind'/'data'/'metadata' objects."
    ),
    include_schema=True,
    include_examples=True,
)


root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    code_executor=code_executor,
    after_agent_callback=generate_memories_callback,
    after_model_callback=a2ui_callback,
    instruction=a2ui_instruction,
    tools=[
        PreloadMemoryTool(),
        generate_dashboard_image,
        get_company_info,
        fetch_live_stock_quote,
        create_research_note,
        get_research_note,
        list_research_notes,
        get_weather,
        get_current_time,
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
)
