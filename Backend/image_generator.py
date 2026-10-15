"""
Image Generation Engine using FLUX.1 [schnell] with 4-bit NF4 Quantization.
Optimized for NVIDIA RTX GPUs with 16GB VRAM on D: drive cache.
"""

import asyncio
import base64
import io
import os
from pathlib import Path
import time
from typing import Any, Dict, Optional

# Ensure Hugging Face loads directly from D: drive cache
os.environ["HF_HOME"] = "D:/hf_cache"

try:
    import torch
    from diffusers import FluxPipeline, FluxTransformer2DModel
    from transformers import BitsAndBytesConfig
    DIFFUSION_AVAILABLE = True
except ImportError:
    torch = None
    DIFFUSION_AVAILABLE = False


BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent if BASE_DIR.name == "Backend" else BASE_DIR

OUTPUT_DIR = BASE_DIR / "generated_images" if (BASE_DIR / "generated_images").exists() else ROOT_DIR / "generated_images"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MODEL_ID = "black-forest-labs/FLUX.1-schnell"
CACHE_DIR = Path("D:/hf_cache")

_flux_pipeline: Optional[Any] = None
_pipeline_lock = asyncio.Lock()


def is_available() -> bool:
    """Check if torch, diffusers, and FLUX model are available."""
    return DIFFUSION_AVAILABLE and torch is not None and torch.cuda.is_available() and CACHE_DIR.exists()


def get_pipeline():
    """Synchronous pipeline loader with 4-bit NF4 quantization for 16GB RTX 4060 Ti."""
    global _flux_pipeline
    if _flux_pipeline is not None:
        return _flux_pipeline

    if not is_available():
        raise RuntimeError("FLUX engine is unavailable. Ensure torch with CUDA and D:/hf_cache are present.")

    print(f"[{time.strftime('%H:%M:%S')}] Loading FLUX.1 [schnell] (4-bit NF4) from {CACHE_DIR}...", flush=True)
    t0 = time.perf_counter()

    # 4-bit quantization config: fits transformer inside ~6-8 GB VRAM
    quantization_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
    )

    transformer = FluxTransformer2DModel.from_pretrained(
        MODEL_ID,
        subfolder="transformer",
        quantization_config=quantization_config,
        torch_dtype=torch.bfloat16,
    )

    pipe = FluxPipeline.from_pretrained(
        MODEL_ID,
        transformer=transformer,
        torch_dtype=torch.bfloat16,
    )
    pipe.enable_model_cpu_offload()

    _flux_pipeline = pipe
    load_time = round(time.perf_counter() - t0, 2)
    print(f"[{time.strftime('%H:%M:%S')}] FLUX.1 [schnell] pipeline loaded in {load_time}s [OK]", flush=True)
    return _flux_pipeline


def _run_inference_sync(
    prompt: str,
    negative_prompt: str = "",
    width: int = 512,
    height: int = 512,
    num_inference_steps: int = 4,
    guidance_scale: float = 0.0,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Synchronous inference worker executed in background thread."""
    pipe = get_pipeline()

    if seed is None or seed < 0:
        actual_seed = torch.randint(0, 2**32 - 1, (1,)).item()
    else:
        actual_seed = int(seed)

    generator = torch.Generator(device="cpu").manual_seed(actual_seed)

    # Restrict width/height to multiples of 16 (required by FLUX DiT)
    width = max(256, min(1536, (width // 16) * 16))
    height = max(256, min(1536, (height // 16) * 16))

    # FLUX schnell operates optimally in 4 steps. If legacy clients send 25, clamp to 4.
    if num_inference_steps > 8:
        num_inference_steps = 4
    else:
        num_inference_steps = max(1, min(8, int(num_inference_steps)))

    # FLUX schnell uses guidance_scale=0.0. If legacy clients send 7.5, clamp to 0.0.
    if guidance_scale > 3.0:
        guidance_scale = 0.0

    t0 = time.perf_counter()

    with torch.inference_mode():
        result = pipe(
            prompt=prompt,
            width=width,
            height=height,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            max_sequence_length=256,
            generator=generator,
        )

    image = result.images[0]
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    file_name = f"flux_{actual_seed}_{int(time.time()*1000)}.png"
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
    num_inference_steps: int = 4,
    guidance_scale: float = 0.0,
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
