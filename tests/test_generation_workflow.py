"""Verify SDXL base, refiner, and upscale workflow construction."""

import pytest

from app.blocks.media_producer import _build_requests
from app.services.generation import GenerationRequest
from app.services.generation.sdxl_workflow import SdxlWorkflowConfig, build_sdxl_workflow


@pytest.fixture
def workflow_config() -> SdxlWorkflowConfig:
    return SdxlWorkflowConfig(
        base_checkpoint="base.safetensors",
        refiner_checkpoint="refiner.safetensors",
        upscale_2x_model="upscale_2x.pth",
        upscale_4x_model="upscale_4x.pth",
    )


def test_media_producer_uses_photorealistic_defaults() -> None:
    request = _build_requests({"positive_prompt": "person posing with a motorcycle"})[0]

    assert request.steps == 69
    assert request.sampler == "dpmpp_2m"
    assert request.scheduler == "karras"


def test_workflow_builds_base_refiner_and_upscale_stages(
    workflow_config: SdxlWorkflowConfig,
) -> None:
    request = GenerationRequest(
        positive_prompt="person riding a motorcycle",
        positive_refiner_prompt="natural skin, realistic hands, detailed leather",
        seed=1234,
    )

    workflow, seed = build_sdxl_workflow(request, "client-id", workflow_config)
    nodes = workflow["prompt"]

    assert seed == 1234
    assert (
        nodes["5"]["inputs"]
        | {
            "steps": 69,
            "sampler_name": "dpmpp_2m",
            "scheduler": "karras",
        }
        == nodes["5"]["inputs"]
    )
    assert (
        nodes["11"]["inputs"]
        | {
            "steps": 20,
            "cfg": 6.0,
            "sampler_name": "dpmpp_2m",
            "scheduler": "karras",
            "denoise": 0.25,
        }
        == nodes["11"]["inputs"]
    )
    assert nodes["14"]["inputs"]["model_name"] == "upscale_2x.pth"
    assert nodes["17"]["inputs"]["model_name"] == "upscale_4x.pth"
    assert nodes["16"]["inputs"]["images"] == ["15", 0]
    assert nodes["19"]["inputs"]["images"] == ["18", 0]


def test_save_image_prefix_includes_output_subdir(
    workflow_config: SdxlWorkflowConfig,
) -> None:
    request = GenerationRequest(
        positive_prompt="person riding a motorcycle",
        extras={"output_subdir": "2026-09-23/cyber-chic/job1"},
    )

    workflow, _ = build_sdxl_workflow(request, "client-id", workflow_config)
    prefixes = [
        node["inputs"]["filename_prefix"]
        for node in workflow["prompt"].values()
        if node["class_type"] == "SaveImage"
    ]

    assert len(prefixes) == 3
    assert all(p.startswith("2026-09-23/cyber-chic/job1/aimw_main") for p in prefixes)


def test_refiner_denoise_must_be_valid_for_comfyui() -> None:
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        SdxlWorkflowConfig(
            base_checkpoint="base.safetensors",
            refiner_checkpoint="refiner.safetensors",
            upscale_2x_model="upscale_2x.pth",
            upscale_4x_model="upscale_4x.pth",
            refiner_denoise=2.0,
        )
