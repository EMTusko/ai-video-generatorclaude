from fastapi import FastAPI, APIRouter, File, UploadFile, HTTPException, Form
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
from datetime import datetime, timezone
import asyncio
import random

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import api_runway  # noqa: E402  (reads env vars loaded above)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Database: MongoDB when MONGO_URL is set, otherwise an in-memory store
# (data is lost on restart, but the app runs with no setup).
mongo_url = os.environ.get('MONGO_URL', '')
if mongo_url and not mongo_url.startswith('memory'):
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(mongo_url)
    logger.info("Using MongoDB at %s", mongo_url.split('@')[-1])
else:
    from mongomock_motor import AsyncMongoMockClient
    client = AsyncMongoMockClient()
    logger.warning("MONGO_URL not set: using an in-memory database (data is lost on restart)")
db = client[os.environ.get('DB_NAME', 'ai_video_generator')]

app = FastAPI(title="AI Video Generator")
app.include_router(api_runway.router, prefix="/api/runway")

api_router = APIRouter(prefix="/api")

uploads_dir = ROOT_DIR / "uploads"
uploads_dir.mkdir(exist_ok=True)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
TEXT_STYLES = {"realistic", "anime", "cartoon", "surreal", "talking_image"}
IMAGE_STYLES = {"character_animation", "movement_overlay", "talking_face"}

# Keep references so background tasks are not garbage-collected mid-run.
_background_tasks = set()


# Models
class VideoGenerationRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)
    style: str  # realistic, anime, cartoon, surreal, talking_image
    duration: int = Field(default=7, ge=5, le=10)
    nsfw_enabled: bool = Field(default=False)

class VideoResponse(BaseModel):
    id: str
    prompt: Optional[str] = None
    image_filename: Optional[str] = None
    style: str
    duration: int
    status: str  # generating, completed, failed
    video_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    error: Optional[str] = None
    mode: str = "demo"  # runway or demo
    created_at: datetime
    nsfw_enabled: bool = Field(default=False)

class UserGallery(BaseModel):
    videos: List[VideoResponse]
    total_count: int

# Sample video URLs and thumbnails for different styles
SAMPLE_VIDEOS = {
    "realistic": [
        {
            "video_url": "https://images.pexels.com/photos/11262264/pexels-photo-11262264.jpeg",
            "thumbnail_url": "https://images.pexels.com/photos/11262264/pexels-photo-11262264.jpeg"
        },
        {
            "video_url": "https://images.pexels.com/photos/7480538/pexels-photo-7480538.jpeg",
            "thumbnail_url": "https://images.pexels.com/photos/7480538/pexels-photo-7480538.jpeg"
        }
    ],
    "anime": [
        {
            "video_url": "https://images.pexels.com/photos/18069362/pexels-photo-18069362.png",
            "thumbnail_url": "https://images.pexels.com/photos/18069362/pexels-photo-18069362.png"
        }
    ],
    "cartoon": [
        {
            "video_url": "https://images.unsplash.com/photo-1733590555923-2aa0e489300e",
            "thumbnail_url": "https://images.unsplash.com/photo-1733590555923-2aa0e489300e"
        }
    ],
    "surreal": [
        {
            "video_url": "https://images.pexels.com/photos/24182512/pexels-photo-24182512.jpeg",
            "thumbnail_url": "https://images.pexels.com/photos/24182512/pexels-photo-24182512.jpeg"
        }
    ],
    "talking_image": [
        {
            "video_url": "https://images.unsplash.com/photo-1717632464005-1f33909e484b",
            "thumbnail_url": "https://images.unsplash.com/photo-1717632464005-1f33909e484b"
        }
    ],
    "character_animation": [
        {
            "video_url": "https://images.pexels.com/photos/17722043/pexels-photo-17722043.jpeg",
            "thumbnail_url": "https://images.pexels.com/photos/17722043/pexels-photo-17722043.jpeg"
        }
    ],
    "movement_overlay": [
        {
            "video_url": "https://images.pexels.com/photos/32539017/pexels-photo-32539017.jpeg",
            "thumbnail_url": "https://images.pexels.com/photos/32539017/pexels-photo-32539017.jpeg"
        }
    ],
    "talking_face": [
        {
            "video_url": "https://images.unsplash.com/photo-1483478550801-ceba5fe50e8e",
            "thumbnail_url": "https://images.unsplash.com/photo-1483478550801-ceba5fe50e8e"
        }
    ]
}


async def run_generation(video_id: str, style: str, prompt: Optional[str],
                         duration: int, image_uri: Optional[str] = None):
    """Generate the video in the background and store the result."""
    try:
        if api_runway.is_enabled():
            video_url = await api_runway.generate_video(prompt, style, duration, image_uri)
            update = {"status": "completed", "video_url": video_url, "thumbnail_url": None}
        else:
            await asyncio.sleep(3)  # demo mode: pretend to work, return sample media
            sample = random.choice(SAMPLE_VIDEOS.get(style, SAMPLE_VIDEOS["realistic"]))
            update = {"status": "completed", **sample}
    except Exception as exc:  # noqa: BLE001 - surface any provider error to the user
        logger.exception("Video %s failed", video_id)
        update = {"status": "failed", "error": str(exc)[:500]}
    await db.videos.update_one({"id": video_id}, {"$set": update})


def start_generation(*args):
    task = asyncio.create_task(run_generation(*args))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def new_video_record(video_id: str, style: str, duration: int, nsfw_enabled: bool, **extra):
    return {
        "id": video_id,
        "style": style,
        "duration": duration,
        "status": "generating",
        "mode": "runway" if api_runway.is_enabled() else "demo",
        "created_at": datetime.now(timezone.utc),
        "nsfw_enabled": nsfw_enabled,
        **extra,
    }


@api_router.post("/generate-text-to-video", response_model=VideoResponse)
async def generate_text_to_video(request: VideoGenerationRequest):
    """Generate video from text prompt"""
    if request.style not in TEXT_STYLES:
        raise HTTPException(status_code=400, detail=f"Unknown style '{request.style}'")
    video_id = str(uuid.uuid4())
    video_data = new_video_record(video_id, request.style, request.duration,
                                  request.nsfw_enabled, prompt=request.prompt)
    await db.videos.insert_one(dict(video_data))
    start_generation(video_id, request.style, request.prompt, request.duration)
    return VideoResponse(**video_data)


@api_router.post("/generate-image-to-video", response_model=VideoResponse)
async def generate_image_to_video(
    file: UploadFile = File(...),
    style: str = Form(...),
    duration: int = Form(7),
    nsfw_enabled: bool = Form(False),
    prompt: Optional[str] = Form(None),
):
    """Generate video from uploaded image"""
    if not (file.content_type or '').startswith('image/'):
        raise HTTPException(status_code=400, detail="File must be an image")
    if style not in IMAGE_STYLES:
        raise HTTPException(status_code=400, detail=f"Unknown style '{style}'")
    if not 5 <= duration <= 10:
        raise HTTPException(status_code=400, detail="Duration must be 5-10 seconds")

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Image must be 10 MB or smaller")

    video_id = str(uuid.uuid4())
    ext = Path(file.filename or '').suffix.lstrip('.').lower()
    if ext not in {"jpg", "jpeg", "png", "gif", "webp"}:
        ext = "jpg"
    filename = f"{video_id}.{ext}"
    (uploads_dir / filename).write_bytes(data)

    video_data = new_video_record(video_id, style, duration, nsfw_enabled,
                                  image_filename=filename, prompt=prompt)
    await db.videos.insert_one(dict(video_data))
    image_uri = api_runway.image_data_uri(data, file.content_type)
    start_generation(video_id, style, prompt, duration, image_uri)
    return VideoResponse(**video_data)

@api_router.get("/video/{video_id}", response_model=VideoResponse)
async def get_video(video_id: str):
    """Get video by ID"""
    video = await db.videos.find_one({"id": video_id})
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    
    return VideoResponse(**video)

@api_router.get("/videos", response_model=UserGallery)
async def get_user_videos(limit: int = 20, offset: int = 0):
    """Get user's video gallery"""
    videos = await db.videos.find().skip(offset).limit(limit).sort("created_at", -1).to_list(limit)
    total_count = await db.videos.count_documents({})
    
    video_responses = [VideoResponse(**video) for video in videos]
    
    return UserGallery(videos=video_responses, total_count=total_count)

@api_router.get("/styles")
async def get_available_styles():
    """Get available video styles"""
    return {
        "text_to_video_styles": [
            {"id": "realistic", "name": "Realistic", "description": "Photorealistic video generation"},
            {"id": "anime", "name": "Anime", "description": "Japanese animation style"},
            {"id": "cartoon", "name": "Cartoon", "description": "Western cartoon style"},
            {"id": "surreal", "name": "Surreal", "description": "Abstract and artistic style"},
            {"id": "talking_image", "name": "Talking Image", "description": "Face animation from image"}
        ],
        "image_to_video_styles": [
            {"id": "character_animation", "name": "Character Animation", "description": "Animate characters in the image"},
            {"id": "movement_overlay", "name": "Movement Overlay", "description": "Add dynamic movement effects"},
            {"id": "talking_face", "name": "Talking Face", "description": "Make faces speak and move"}
        ]
    }

# Include the router in the main app
app.include_router(api_router)

app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

allowed_origins = [o.strip() for o in os.environ.get('ALLOWED_ORIGINS', '*').split(',') if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_credentials=allowed_origins != ['*'],
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the built React app from the same server when it exists
# (run `yarn build` in frontend/), so the whole app is on one port.
frontend_build = ROOT_DIR.parent / "frontend" / "build"
if frontend_build.is_dir():
    app.mount("/", StaticFiles(directory=frontend_build, html=True), name="frontend")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
