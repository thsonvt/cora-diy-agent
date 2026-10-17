"""Local browser UI for the exported Cora agent. Run: python cora-web.py."""

import argparse
import asyncio
import importlib.util
import json
import logging
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Annotated
from uuid import uuid4

import uvicorn
from agent_framework import AgentSession, Content, Message
from azure.core.exceptions import ClientAuthenticationError
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cora_agent", ROOT / "cora-app.py")
cora = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cora)
logger = logging.getLogger("cora.web")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SESSION_TTL = 60 * 60


@dataclass
class Conversation:
    session: AgentSession
    last_used: float = field(default_factory=monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turns: int = 0


@asynccontextmanager
async def lifespan(app):
    async with cora.create_agent() as agent:
        app.state.agent = agent
        app.state.conversations = {}
        yield
        app.state.conversations.clear()


app = FastAPI(title="Cora · Zava", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def home():
    return FileResponse(ROOT / "static/cora.html", headers={"Cache-Control": "no-store"})


@app.post("/api/conversations", status_code=201)
async def new_conversation():
    conversations = app.state.conversations
    for key, conversation in list(conversations.items()):
        if monotonic() - conversation.last_used > SESSION_TTL and not conversation.lock.locked():
            del conversations[key]
    if len(conversations) >= 100:
        raise HTTPException(429, "Too many open chats. Close an existing chat or restart the server.")
    conversation_id = str(uuid4())
    conversations[conversation_id] = Conversation(app.state.agent.create_session())
    return {"id": conversation_id}


def get_conversation(conversation_id):
    conversation = app.state.conversations.get(conversation_id)
    if conversation is None or monotonic() - conversation.last_used > SESSION_TTL:
        raise HTTPException(404, "This chat has expired. Start a new chat to continue.")
    return conversation


async def reserve_conversation(conversation_id: str):
    conversation = get_conversation(conversation_id)
    if conversation.lock.locked():
        raise HTTPException(409, "Cora is still replying. Please wait a moment.")
    # FastAPI keeps this reservation through upload validation and streaming.
    async with conversation.lock:
        conversation.last_used = monotonic()
        try:
            yield conversation
        finally:
            conversation.last_used = monotonic()


@app.delete("/api/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str):
    conversation = get_conversation(conversation_id)
    if conversation.lock.locked():
        raise HTTPException(409, "Wait for Cora to finish before starting a new chat.")
    del app.state.conversations[conversation_id]


async def image_content(image):
    try:
        data = await image.read(MAX_IMAGE_BYTES + 1)
    finally:
        await image.close()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Choose an image smaller than 5 MB.")
    signatures = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    if not signatures.get(image.content_type):
        raise HTTPException(415, "Choose a PNG, JPEG, or WebP image.")
    return Content.from_data(data=data, media_type=image.content_type)


@app.post("/api/conversations/{conversation_id}/messages")
async def chat(
    conversation_id: str,
    conversation: Annotated[Conversation, Depends(reserve_conversation)],
    message: Annotated[str, Form(min_length=1, max_length=4000)],
    image: Annotated[UploadFile | None, File()] = None,
):
    if not message.strip():
        raise HTTPException(422, "Tell Cora a little about your project first.")
    if conversation.turns >= 40:
        raise HTTPException(409, "This chat has reached 40 messages. Start a new chat to continue.")
    contents = [Content.from_text(message.strip())]
    if image is not None:
        contents.append(await image_content(image))

    def event(kind, **values):
        return json.dumps({"type": kind, **values}) + "\n"

    async def reply():
        try:
            # Failed or disconnected turns must not leave partial tool history.
            session = AgentSession.from_dict(deepcopy(conversation.session.to_dict()))
            yield event("status", text="Cora is thinking…")
            async with asyncio.timeout(120):
                async for chunk in app.state.agent.run(
                    Message(role="user", contents=contents), session=session, stream=True,
                ):
                    if any(content.type == "function_call" for content in chunk.contents):
                        yield event("status", text="Checking Zava’s catalog…")
                    for content in chunk.contents:
                        if content.type == "text":
                            replacement = content.merge_mode == "replace" or chunk.merge_mode == "replace"
                            yield event("replace" if replacement else "text", text=content.text)
            conversation.session = session
            conversation.turns += 1
            yield event("done")
        except TimeoutError:
            logger.exception("Cora reply timed out")
            yield event("error", text="Cora took too long to reply. Please try sending your message again.")
        except Exception as error:
            logger.exception("Cora reply failed")
            if isinstance(error, ClientAuthenticationError) or isinstance(error.__cause__, ClientAuthenticationError):
                text = "Cora needs an Azure sign-in. Run az login in the terminal environment where the server runs, then send your message again."
            else:
                text = "Cora couldn’t finish that reply. Check the server terminal for details, then try again."
            yield event("error", text=text)

    return StreamingResponse(
        reply(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    options = parser.parse_args()
    uvicorn.run(app, host=options.host, port=options.port)
