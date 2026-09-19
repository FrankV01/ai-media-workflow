"""Build ComfyUI SDXL base, refiner, and image-upscale workflow payloads."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.services.generation.base import GenerationRequest


@dataclass(frozen=True)
class SdxlWorkflowConfig:
    """Models and sampling parameters used after the base generation pass."""

    base_checkpoint: str
    refiner_checkpoint: str
    upscale_2x_model: str
    upscale_4x_model: str
    refiner_steps: int = 20
    refiner_cfg_scale: float = 6.0
    refiner_sampler: str = "dpmpp_2m"
    refiner_scheduler: str = "karras"
    refiner_denoise: float = 0.25

    def __post_init__(self) -> None:
        if not 0.0 <= self.refiner_denoise <= 1.0:
            raise ValueError("refiner_denoise must be between 0.0 and 1.0")


def build_sdxl_workflow(
    request: GenerationRequest,
    client_id: str,
    config: SdxlWorkflowConfig,
) -> tuple[dict[str, Any], int]:
    """Build one graph that emits refined 1x, model-upscaled 2x, and 4x images."""
    seed = request.seed if request.seed >= 0 else int(uuid.uuid4().int % (2**32))
    positive_refiner = request.positive_refiner_prompt or request.positive_prompt
    negative_refiner = request.negative_refiner_prompt or request.negative_prompt
    prefix = f"aimw_{request.variant_name}"

    prompt = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": request.extras.get("checkpoint", config.base_checkpoint)},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": request.positive_prompt, "clip": ["1", 1]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": request.negative_prompt, "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": request.width, "height": request.height, "batch_size": 1},
        },
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
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": config.refiner_checkpoint},
        },
        "8": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": positive_refiner, "clip": ["7", 1]},
        },
        "9": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_refiner, "clip": ["7", 1]},
        },
        "10": {
            "class_type": "VAEEncode",
            "inputs": {"pixels": ["6", 0], "vae": ["7", 2]},
        },
        "11": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["7", 0],
                "positive": ["8", 0],
                "negative": ["9", 0],
                "latent_image": ["10", 0],
                "seed": seed,
                "steps": config.refiner_steps,
                "cfg": config.refiner_cfg_scale,
                "sampler_name": config.refiner_sampler,
                "scheduler": config.refiner_scheduler,
                "denoise": config.refiner_denoise,
            },
        },
        "12": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["11", 0], "vae": ["7", 2]},
        },
        "13": {
            "class_type": "SaveImage",
            "inputs": {"images": ["12", 0], "filename_prefix": f"{prefix}_refined_1x"},
        },
        "14": {
            "class_type": "UpscaleModelLoader",
            "inputs": {"model_name": config.upscale_2x_model},
        },
        "15": {
            "class_type": "ImageUpscaleWithModel",
            "inputs": {"upscale_model": ["14", 0], "image": ["12", 0]},
        },
        "16": {
            "class_type": "SaveImage",
            "inputs": {"images": ["15", 0], "filename_prefix": f"{prefix}_upscaled_2x"},
        },
        "17": {
            "class_type": "UpscaleModelLoader",
            "inputs": {"model_name": config.upscale_4x_model},
        },
        "18": {
            "class_type": "ImageUpscaleWithModel",
            "inputs": {"upscale_model": ["17", 0], "image": ["12", 0]},
        },
        "19": {
            "class_type": "SaveImage",
            "inputs": {"images": ["18", 0], "filename_prefix": f"{prefix}_upscaled_4x"},
        },
    }
    return {"client_id": client_id, "prompt": prompt}, seed
