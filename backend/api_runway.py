"""Runway video generation.

When RUNWAY_API_KEY is set, videos are generated with the Runway API.
Without it the app runs in demo mode and returns sample media instead.
"""
import asyncio
import base64
import logging
import os
from typing import Optional

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()

TEXT_MODEL = os.getenv("RUNWAY_TEXT_MODEL", "gen4.5")
IMAGE_MODEL = os.getenv("RUNWAY_IMAGE_MODEL", "gen4.5")
RATIO = os.getenv("RUNWAY_RATIO", "1280:720")
POLL_SECONDS = float(os.getenv("RUNWAY_POLL_SECONDS", "5"))
TIMEOUT_SECONDS = float(os.getenv("RUNWAY_TIMEOUT_SECONDS", "600"))

STYLE_HINTS = {
    "realistic": "photorealistic, cinematic lighting",
    "anime": "Japanese anime style",
    "cartoon": "western cartoon style, bold outlines",
    "surreal": "surreal, dreamlike, abstract art",
    "talking_image": "close-up portrait of a person talking naturally",
    "character_animation": "animate the characters with natural motion",
    "movement_overlay": "add dynamic camera movement and motion effects",
    "talking_face": "the person speaks with natural facial movement",
}


def api_key() -> Optional[str]:
    return os.getenv("RUNWAY_API_KEY") or os.getenv("RUNWAYML_API_SECRET")


def is_enabled() -> bool:
    return bool(api_key())


def build_prompt(prompt: Optional[str], style: str) -> str:
    hint = STYLE_HINTS.get(style, "")
    text = ", ".join(p for p in (prompt or "", hint) if p)
    return text[:1000] or "cinematic video"


def image_data_uri(data: bytes, content_type: str) -> str:
    return f"data:{content_type};base64,{base64.b64encode(data).decode()}"


async def _wait_for_task(client, task_id: str) -> str:
    waited = 0.0
    while waited < TIMEOUT_SECONDS:
        task = await client.tasks.retrieve(task_id)
        if task.status == "SUCCEEDED":
            return task.output[0]
        if task.status in ("FAILED", "CANCELLED"):
            raise RuntimeError(getattr(task, "failure", None) or f"Runway task {task.status}")
        await asyncio.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    raise TimeoutError("Runway task timed out")


async def generate_video(
    prompt: Optional[str],
    style: str,
    duration: int,
    image_uri: Optional[str] = None,
) -> str:
    """Start a Runway task, wait for it, and return the video URL."""
    from runwayml import AsyncRunwayML

    client = AsyncRunwayML(api_key=api_key())
    prompt_text = build_prompt(prompt, style)
    if image_uri:
        task = await client.image_to_video.create(
            model=IMAGE_MODEL,
            prompt_image=image_uri,
            prompt_text=prompt_text,
            ratio=RATIO,
            duration=duration,
        )
    else:
        task = await client.text_to_video.create(
            model=TEXT_MODEL,
            prompt_text=prompt_text,
            ratio=RATIO,
            duration=duration,
        )
    logger.info("Runway task %s started", task.id)
    return await _wait_for_task(client, task.id)


@router.get("/status")
async def runway_status():
    """Report whether real Runway generation is configured."""
    return {
        "mode": "runway" if is_enabled() else "demo",
        "text_model": TEXT_MODEL,
        "image_model": IMAGE_MODEL,
        "ratio": RATIO,
    }
