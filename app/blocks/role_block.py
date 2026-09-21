"""
app.blocks.role_block — Base class for LLM-powered creative role blocks

Every "employee" in the creative agency pipeline is a RoleBlock subclass.
Each role reads a brief from context, calls an LLM with a role-specific
system prompt, and writes its output into the context for the next role.

Subclasses define: meta, role_name, role_title, role_description,
system_prompt, and optionally suggested_next and default_temperature.
See agents.md § "RoleBlock interface" for the full pattern.

The base class handles: LLM API calls (OpenAI-compatible), reasoning control
(reasoning_effort="none" unless LLM_ENABLE_THINKING is set), token tracking,
and context threading (reads "brief", writes "brief", "output_deliverable",
"suggested_next_role", and "{role_name}_output"). Empty LLM content raises
ValueError so the step fails instead of passing an empty brief downstream.

Each call is also appended to context["_executions"] as a record shaped for
the RoleExecution/Message models; nothing persists those records yet.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from openai import AsyncOpenAI

from app.blocks.base import Block
from app.config import settings
from app.services.workload_guard import workload_guard

logger = logging.getLogger(__name__)


class RoleBlock(Block):
    """
    Abstract base for all creative role blocks.

    Subclasses must set:
        role_name: str
        role_title: str
        role_description: str
        system_prompt: str
    """

    role_name: str
    role_title: str
    role_description: str
    system_prompt: str
    suggested_next: str | None = None
    output_format: str = "text"
    default_temperature: float | None = None

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Execute this LLM role under exclusive workload ownership."""
        async with workload_guard.hold(f"llm-role-{self.role_name}"):
            return await self._run_unlocked(context)

    async def _run_unlocked(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Execute the role: call LLM with system prompt + input brief,
        persist everything, return the deliverable for the next role.
        """
        input_brief = context.get("brief", context.get("input_brief", ""))
        if not input_brief:
            raise ValueError(f"{self.role_name}: No input brief found in context")

        # Determine LLM parameters (role override > config defaults)
        model = settings.llm_model
        temperature = self.default_temperature or settings.llm_temperature
        max_tokens = settings.llm_max_tokens

        # Per-role overrides (system_prompt / model_override / temperature).
        # Intended to come from the CreativeRole table; nothing populates this yet.
        role_overrides = context.get("_role_overrides", {}).get(self.role_name, {})
        effective_prompt = role_overrides.get("system_prompt", self.system_prompt)
        if role_overrides.get("model_override"):
            model = role_overrides["model_override"]
        if role_overrides.get("temperature") is not None:
            temperature = role_overrides["temperature"]

        # Build messages
        messages = [
            {"role": "system", "content": effective_prompt},
            {"role": "user", "content": input_brief},
        ]

        # Call LLM (OpenAI-compatible API — works with LM Studio, OpenAI, etc.)
        thinking = settings.llm_enable_thinking
        logger.info(
            "Role [%s] calling %s at %s (thinking=%s) …",
            self.role_name,
            model,
            settings.llm_base_url,
            thinking,
        )
        client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.openai_api_key,
            timeout=settings.llm_timeout,
        )

        create_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        reasoning_effort = None if thinking else "none"
        if reasoning_effort is not None:
            # Disables hidden reasoning on thinking models (LM Studio honors
            # reasoning_effort="none"); when enabled, omit and let the server default apply
            create_kwargs["reasoning_effort"] = reasoning_effort

        started_at = datetime.now(UTC)
        execution_record = {
            "role_name": self.role_name,
            "role_title": self.role_title,
            "role_description": self.role_description,
            "system_prompt": effective_prompt,
            "output_format": self.output_format,
            "input_brief": input_brief,
            "output_deliverable": None,
            "suggested_next_role": self.suggested_next,
            "model_used": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "reasoning_effort": reasoning_effort,
            "finish_reason": None,
            "status": "running",
            "error_type": None,
            "error": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "started_at": started_at,
            "finished_at": None,
            "messages": [
                {"role": "system", "content": effective_prompt, "ordinal": 0},
                {"role": "user", "content": input_brief, "ordinal": 1},
            ],
        }
        executions = context.setdefault("_executions", [])

        try:
            response = await client.chat.completions.create(**create_kwargs)

            assistant_content = response.choices[0].message.content or ""
            finish_reason = response.choices[0].finish_reason
            usage = response.usage
            execution_record.update(
                {
                    "output_deliverable": assistant_content,
                    "finish_reason": finish_reason,
                    "prompt_tokens": usage.prompt_tokens if usage else None,
                    "completion_tokens": usage.completion_tokens if usage else None,
                    "total_tokens": usage.total_tokens if usage else None,
                }
            )
            execution_record["messages"].append(
                {"role": "assistant", "content": assistant_content, "ordinal": 2}
            )

            logger.info(
                "Role [%s] completed — %d tokens (finish_reason=%s)",
                self.role_name,
                usage.total_tokens if usage else 0,
                finish_reason,
            )

            if not assistant_content.strip():
                hint = ""
                if finish_reason == "length":
                    hint = (
                        " — the model likely exhausted max_tokens (possibly on reasoning); "
                        "raise LLM_MAX_TOKENS or use a smaller reasoning budget"
                    )
                raise ValueError(
                    f"{self.role_name}: LLM returned empty content "
                    f"(finish_reason={finish_reason}, "
                    f"completion_tokens={usage.completion_tokens if usage else 0}){hint}"
                )
        except Exception as exc:
            execution_record.update(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "finished_at": datetime.now(UTC),
                }
            )
            executions.append(execution_record)
            raise

        execution_record.update({"status": "completed", "finished_at": datetime.now(UTC)})
        executions.append(execution_record)

        # Determine suggested next role
        next_role = self.suggested_next

        return {
            "brief": assistant_content,  # next role reads this as its input
            "output_deliverable": assistant_content,
            "suggested_next_role": next_role,
            "_executions": executions,
            f"{self.role_name}_output": assistant_content,  # role-specific key
        }

    async def validate(self, context: dict[str, Any]) -> None:
        """Check that the LLM endpoint is configured."""
        if not settings.llm_base_url:
            raise ValueError(
                f"{self.role_name}: LLM_BASE_URL is not set. "
                "Add it to your .env file (default: http://127.0.0.1:1234/v1 for LM Studio)."
            )
