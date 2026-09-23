"""
app.services.generation.comfyui — ComfyUI API backend

Dispatches image generation to a running ComfyUI server by:
1. Building an SDXL txt2img workflow JSON with prompt data injected
2. POSTing to /prompt
3. Polling /history/{prompt_id} until complete
4. Downloading output image(s) to
   IMAGE_OUTPUT_DIR/<yyyy-mm-dd>/<shoot-slug>/job<id>/

AI model/workflow settings (checkpoints, refiner sampling, upscale models)
are injected via SdxlWorkflowConfig — typically resolved from the media
model configuration profile. Operational settings (URL, poll interval,
timeout, output dir) remain env-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

import httpx

from app.config import settings
from app.services.generation.base import GenerationBackend, GenerationRequest, GenerationResult
from app.services.generation.sdxl_workflow import SdxlWorkflowConfig, build_sdxl_workflow
from app.services.workload_guard import workload_guard

logger = logging.getLogger(__name__)


class ComfyUIBackend(GenerationBackend):
    """Dispatches generation to a ComfyUI server via its REST API."""

    name = "comfyui"

    def __init__(self, workflow_config: SdxlWorkflowConfig | None = None) -> None:
        self.base_url = settings.comfyui_url.rstrip("/")
        self.poll_interval = settings.comfyui_poll_interval
        self.timeout = settings.comfyui_timeout
        # Resolved profile normally supplies this; fall back to code/env
        # defaults so no-arg construction still works (startup checks, tests).
        self.workflow_config = workflow_config or SdxlWorkflowConfig(
            base_checkpoint=settings.comfyui_checkpoint,
            refiner_checkpoint=settings.comfyui_refiner_checkpoint,
            upscale_2x_model=settings.comfyui_upscale_2x_model,
            upscale_4x_model=settings.comfyui_upscale_4x_model,
            refiner_steps=settings.refiner_steps,
            refiner_cfg_scale=settings.refiner_cfg_scale,
            refiner_sampler=settings.refiner_sampler,
            refiner_scheduler=settings.refiner_scheduler,
            refiner_denoise=settings.refiner_denoise,
        )

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        async with workload_guard.hold(f"comfyui-{request.variant_name}"):
            return await self._generate_unlocked(request)

    async def _generate_unlocked(self, request: GenerationRequest) -> GenerationResult:
        start = time.monotonic()
        client_id = str(uuid.uuid4())

        # Allow custom workflow JSON from extras
        custom_workflow_path = request.extras.get("workflow_path")
        if custom_workflow_path:
            workflow = self._load_custom_workflow(custom_workflow_path, request, client_id)
            seed = request.seed if request.seed >= 0 else 42
        else:
            workflow, seed = build_sdxl_workflow(request, client_id, self.workflow_config)

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Submit the prompt
            logger.info("ComfyUI: submitting prompt to %s/prompt", self.base_url)
            resp = await client.post(f"{self.base_url}/prompt", json=workflow)
            resp.raise_for_status()
            prompt_id = resp.json()["prompt_id"]
            logger.info("ComfyUI: prompt_id=%s, polling for completion…", prompt_id)

            # Poll for completion
            output_subdir = request.extras.get("output_subdir")
            image_paths = await self._poll_until_done(client, prompt_id, output_subdir)

        elapsed = time.monotonic() - start
        logger.info("ComfyUI: generation complete in %.1fs, %d image(s)", elapsed, len(image_paths))

        return GenerationResult(
            image_paths=image_paths,
            seed_used=seed,
            backend_name=self.name,
            generation_time_seconds=elapsed,
            metadata={
                "prompt_id": prompt_id,
                "comfyui_url": self.base_url,
                "width": request.width,
                "height": request.height,
                "output_scales": [1, 2, 4],
                "refiner_checkpoint": self.workflow_config.refiner_checkpoint,
                "refiner_steps": self.workflow_config.refiner_steps,
                "refiner_cfg_scale": self.workflow_config.refiner_cfg_scale,
                "refiner_sampler": self.workflow_config.refiner_sampler,
                "refiner_scheduler": self.workflow_config.refiner_scheduler,
                "refiner_denoise": self.workflow_config.refiner_denoise,
            },
        )

    async def _poll_until_done(
        self,
        client: httpx.AsyncClient,
        prompt_id: str,
        output_subdir: str | None = None,
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
                    local_path = await self._download_image(client, img_info, output_subdir)
                    if local_path:
                        image_paths.append(local_path)

            return image_paths

        raise TimeoutError(
            f"ComfyUI generation timed out after {self.timeout}s for prompt {prompt_id}"
        )

    async def _download_image(
        self,
        client: httpx.AsyncClient,
        img_info: dict,
        output_subdir: str | None = None,
    ) -> Path | None:
        """Download a generated image from ComfyUI to local storage."""
        filename = img_info.get("filename")
        subfolder = img_info.get("subfolder", "")
        img_type = img_info.get("type", "output")

        if not filename:
            return None

        output_dir = Path(settings.image_output_dir)
        if output_subdir:
            output_dir = output_dir / output_subdir
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
