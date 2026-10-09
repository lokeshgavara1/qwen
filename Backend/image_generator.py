"""
Image Generation Engine using Stable Diffusion v1.5 Safetensors.
Supports GPU (CUDA) acceleration with torch.float16 and attention slicing for optimal VRAM usage.
"""

import asyncio
import base64
import io
import os
from pathlib import Path
import time
from typing import Any, Dict, Optional, Tuple

import torch

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent if BASE_DIR.name == "Backend" else BASE_DIR

DEFAULT_MODEL_PATH = BASE_DIR / "Image-Generation" / "v1-5-pruned.safetensors"
if not DEFAULT_MODEL_PATH.exists():
    DEFAULT_MODEL_PATH = ROOT_DIR / "Image-Generation" / "v1-5-pruned.safetensors"

DIFFUSION_MODEL_PATH = os.getenv("DIFFUSION_MODEL_PATH", str(DEFAULT_MODEL_PATH))
OUTPUT_DIR = BASE_DIR / "generated_images" if (BASE_DIR / "generated_images").exists() else ROOT_DIR / "generated_images"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

_sd_pipeline: Optional[Any] = None
_pipeline_lock = asyncio.Lock()
DIFFUSION_AVAILABLE = False

try:
    from diffusers import StableDiffusionPipeline, DPMSolverMultistepScheduler
    DIFFUSION_AVAILABLE = True
except ImportError:
    DIFFUSION_AVAILABLE = False


def is_available() -> bool:
    """Check if diffusers and model file are present."""
    return DIFFUSION_AVAILABLE and Path(DIFFUSION_MODEL_PATH).exists()


def get_pipeline():
    """Synchronous pipeline loader with optimizations for RTX GPUs."""
    global _sd_pipeline
    if _sd_pipeline is not None:
        return _sd_pipeline

    if not is_available():
        raise RuntimeError(
            f"Diffusion pipeline unavailable. diffusers={DIFFUSION_AVAILABLE}, file_exists={Path(DIFFUSION_MODEL_PATH).exists()} ({DIFFUSION_MODEL_PATH})"
        )

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32

    print(f"[{time.strftime('%H:%M:%S')}] Loading Stable Diffusion pipeline from {DIFFUSION_MODEL_PATH} ({device}, {dtype})...", flush=True)
    t0 = time.perf_counter()

    pipe = StableDiffusionPipeline.from_single_file(
        DIFFUSION_MODEL_PATH,
        torch_dtype=dtype,
        use_safetensors=True,
        safety_checker=None,
        requires_safety_checker=False,
    )

    # Use DPM-Solver++ for faster convergence (15-25 steps is enough for high quality)
    try:
        pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config, use_karras_sigmas=True)
    except Exception:
        pass

    pipe = pipe.to(device)

    if device == "cuda":
        try:
            pipe.enable_attention_slicing()
        except Exception:
            pass

    _sd_pipeline = pipe
    load_time = round(time.perf_counter() - t0, 2)
    print(f"[{time.strftime('%H:%M:%S')}] Stable Diffusion pipeline loaded in {load_time}s [OK]", flush=True)
    return _sd_pipeline


def _run_inference_sync(
    prompt: str,
    negative_prompt: str = "",
    width: int = 512,
    height: int = 512,
    num_inference_steps: int = 25,
    guidance_scale: float = 7.5,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Synchronous inference worker executed in background thread."""
    pipe = get_pipeline()
    device = "cuda" if torch.cuda.is_available() else "cpu"

    if seed is None or seed < 0:
        generator = None
        actual_seed = torch.randint(0, 2**32 - 1, (1,)).item()
    else:
        actual_seed = int(seed)
        generator = torch.Generator(device=device).manual_seed(actual_seed)

    # Restrict width/height to multiples of 8 and reasonable limits
    width = max(256, min(1024, (width // 8) * 8))
    height = max(256, min(1024, (height // 8) * 8))
    num_inference_steps = max(10, min(50, int(num_inference_steps)))

    t0 = time.perf_counter()

    with torch.inference_mode():
        result = pipe(
            prompt=prompt,
            negative_prompt=negative_prompt or "ugly, blurry, bad anatomy, bad hands, cropped, low quality, artifact",
            width=width,
            height=height,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            generator=generator,
        )

    image = result.images[0]
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    file_name = f"img_{actual_seed}_{int(time.time()*1000)}.png"
    file_path = OUTPUT_DIR / file_name
    image.save(file_path, format="PNG")
    rel_url = f"/generated_images/{file_name}"

    # Also keep Base64 data for standalone API clients
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    b64_data = base64.b64encode(buffer.getvalue()).decode("utf-8")
    data_uri = f"data:image/png;base64,{b64_data}"

    return {
        "image_base64": b64_data,
        "image_url": rel_url,
        "data_uri": data_uri,
        "file_path": str(file_path),
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "seed": actual_seed,
        "width": width,
        "height": height,
        "steps": num_inference_steps,
        "guidance_scale": guidance_scale,
        "latency_ms": elapsed_ms,
    }


async def generate_image_async(
    prompt: str,
    negative_prompt: str = "",
    width: int = 512,
    height: int = 512,
    num_inference_steps: int = 25,
    guidance_scale: float = 7.5,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Asynchronous wrapper that offloads heavy diffusion generation to threadpool."""
    async with _pipeline_lock:
        return await asyncio.to_thread(
            _run_inference_sync,
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            seed=seed,
        )
