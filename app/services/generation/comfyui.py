"""
app.services.generation.comfyui — ComfyUI API backend

Dispatches image generation to a running ComfyUI server by:
1. Building an SDXL txt2img workflow JSON with prompt data injected
2. POSTing to /prompt
3. Polling /history/{prompt_id} until complete
4. Downloading output image(s) to IMAGE_OUTPUT_DIR

Checkpoint is configurable via COMFYUI_CHECKPOINT in settings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from pathlib import Path

import httpx

from app.config import settings
from app.services.generation.base import GenerationBackend, GenerationRequest, GenerationResult

logger = logging.getLogger(__name__)


def _build_sdxl_workflow(request: GenerationRequest, client_id: str) -> dict:
    """Build a minimal SDXL txt2img ComfyUI workflow API payload.

    This produces the standard node graph:
    CheckpointLoader → CLIPTextEncode (pos) → KSampler → VAEDecode → SaveImage
                     → CLIPTextEncode (neg) ↗
    """
    seed = request.seed if request.seed >= 0 else int(uuid.uuid4().int % (2**32))

    workflow = {
        "client_id": client_id,
        "prompt": {
            # Checkpoint loader
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {
                    "ckpt_name": request.extras.get("checkpoint", settings.comfyui_checkpoint),
                },
            },
            # Positive CLIP encode
            "2": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": request.positive_prompt,
                    "clip": ["1", 1],
                },
            },
            # Negative CLIP encode
            "3": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": request.negative_prompt,
                    "clip": ["1", 1],
                },
            },
            # Empty latent image
            "4": {
                "class_type": "EmptyLatentImage",
                "inputs": {
                    "width": request.width,
                    "height": request.height,
                    "batch_size": 1,
                },
            },
            # KSampler
            "5": {
                "class_type": "KSampler",
                "inputs": {
                    "model": ["1", 0],
                    "positive": ["2", 0],
                    "negative": ["3", 0],
                    "latent_image": ["4", 0],
                    "seed": seed,
                    "steps": request.steps,
                    "cfg": request.cfg_scale,
                    "sampler_name": request.sampler,
                    "scheduler": request.scheduler,
                    "denoise": 1.0,
                },
            },
            # VAE Decode
            "6": {
                "class_type": "VAEDecode",
                "inputs": {
                    "samples": ["5", 0],
                    "vae": ["1", 2],
                },
            },
            # Save Image
            "7": {
                "class_type": "SaveImage",
                "inputs": {
                    "images": ["6", 0],
                    "filename_prefix": f"aimw_{request.variant_name}",
                },
            },
        },
    }

    return workflow


class ComfyUIBackend(GenerationBackend):
    """Dispatches generation to a ComfyUI server via its REST API."""

    name = "comfyui"

    def __init__(self) -> None:
        self.base_url = settings.comfyui_url.rstrip("/")
        self.poll_interval = settings.comfyui_poll_interval
        self.timeout = settings.comfyui_timeout

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        start = time.monotonic()
        client_id = str(uuid.uuid4())

        # Allow custom workflow JSON from extras
        custom_workflow_path = request.extras.get("workflow_path")
        if custom_workflow_path:
            workflow = self._load_custom_workflow(custom_workflow_path, request, client_id)
        else:
            workflow = _build_sdxl_workflow(request, client_id)

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Submit the prompt
            logger.info("ComfyUI: submitting prompt to %s/prompt", self.base_url)
            resp = await client.post(f"{self.base_url}/prompt", json=workflow)
            resp.raise_for_status()
            prompt_id = resp.json()["prompt_id"]
            logger.info("ComfyUI: prompt_id=%s, polling for completion…", prompt_id)

            # Poll for completion
            image_paths = await self._poll_until_done(client, prompt_id)

        elapsed = time.monotonic() - start
        logger.info("ComfyUI: generation complete in %.1fs, %d image(s)", elapsed, len(image_paths))

        return GenerationResult(
            image_paths=image_paths,
            seed_used=request.seed,
            backend_name=self.name,
            generation_time_seconds=elapsed,
            metadata={
                "prompt_id": prompt_id,
                "comfyui_url": self.base_url,
                "width": request.width,
                "height": request.height,
            },
        )

    async def _poll_until_done(
        self, client: httpx.AsyncClient, prompt_id: str
    ) -> list[Path]:
        """Poll /history/{prompt_id} until the job finishes or times out."""
        deadline = time.monotonic() + self.timeout

        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_interval)

            resp = await client.get(f"{self.base_url}/history/{prompt_id}")
            resp.raise_for_status()
            history = resp.json()

            if prompt_id not in history:
                continue

            outputs = history[prompt_id].get("outputs", {})
            image_paths: list[Path] = []

            for node_id, node_output in outputs.items():
                images = node_output.get("images", [])
                for img_info in images:
                    # Download the image from ComfyUI
                    local_path = await self._download_image(client, img_info)
                    if local_path:
                        image_paths.append(local_path)

            return image_paths

        raise TimeoutError(
            f"ComfyUI generation timed out after {self.timeout}s for prompt {prompt_id}"
        )

    async def _download_image(
        self, client: httpx.AsyncClient, img_info: dict
    ) -> Path | None:
        """Download a generated image from ComfyUI to local storage."""
        filename = img_info.get("filename")
        subfolder = img_info.get("subfolder", "")
        img_type = img_info.get("type", "output")

        if not filename:
            return None

        output_dir = Path(settings.image_output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        local_path = output_dir / filename

        params = {"filename": filename, "subfolder": subfolder, "type": img_type}
        resp = await client.get(f"{self.base_url}/view", params=params)
        resp.raise_for_status()

        local_path.write_bytes(resp.content)
        logger.info("ComfyUI: saved %s (%d bytes)", local_path, len(resp.content))
        return local_path

    def _load_custom_workflow(
        self, workflow_path: str, request: GenerationRequest, client_id: str
    ) -> dict:
        """Load a custom ComfyUI workflow JSON and inject prompt data."""
        path = Path(workflow_path)
        if not path.exists():
            raise FileNotFoundError(f"Custom workflow not found: {path}")

        workflow = json.loads(path.read_text())

        # Inject client_id at top level
        workflow["client_id"] = client_id

        # Simple string replacement for prompt injection
        prompt_nodes = workflow.get("prompt", workflow)
        raw = json.dumps(prompt_nodes)
        raw = raw.replace("{{positive_prompt}}", request.positive_prompt)
        raw = raw.replace("{{negative_prompt}}", request.negative_prompt)
        raw = raw.replace("{{positive_refiner_prompt}}", request.positive_refiner_prompt)
        raw = raw.replace("{{negative_refiner_prompt}}", request.negative_refiner_prompt)
        raw = raw.replace("{{width}}", str(request.width))
        raw = raw.replace("{{height}}", str(request.height))
        raw = raw.replace("{{steps}}", str(request.steps))
        raw = raw.replace("{{cfg_scale}}", str(request.cfg_scale))
        raw = raw.replace("{{seed}}", str(request.seed if request.seed >= 0 else 42))

        if "prompt" in workflow:
            workflow["prompt"] = json.loads(raw)
        else:
            workflow = {"client_id": client_id, "prompt": json.loads(raw)}

        return workflow

    async def is_available(self) -> bool:
        """Check if the ComfyUI server is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/system_stats")
                return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False
