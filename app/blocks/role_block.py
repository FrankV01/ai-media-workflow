"""
app.blocks.role_block — Base class for LLM-powered creative role blocks

Every "employee" in the creative agency pipeline is a RoleBlock subclass.
Each role:
1. Reads an input brief from the pipeline context
2. Calls an LLM with a role-specific system prompt
3. Persists the full conversation (system + user + assistant) to the DB
4. Writes its output deliverable into the context for the next role

Subclasses only need to define:
- meta (BlockMeta)
- role_name: str          — matches CreativeRole.name in the DB
- system_prompt: str      — default prompt (can be overridden via DB)
- role_title: str         — human-friendly title
- role_description: str   — what this role does

The base class handles:
- LLM API calls (OpenAI)
- DB role creation/lookup (auto-seeds on first run)
- RoleExecution + Message persistence
- Token tracking
- Context threading (reads input_brief, writes output_deliverable + suggested_next_role)

Future enhancements:
- Streaming responses
- Multi-turn conversations (follow-up questions)
- Human-in-the-loop approval before passing to next role
- Tool use / function calling for structured output
- Support for additional providers (Anthropic, Ollama, etc.)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from openai import AsyncOpenAI

from app.blocks.base import Block
from app.config import settings

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

        # Check for DB-stored role overrides in context (populated by engine)
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
        logger.info("Role [%s] calling %s at %s …", self.role_name, model, settings.llm_base_url)
        client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.openai_api_key,
        )

        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        assistant_content = response.choices[0].message.content or ""
        usage = response.usage

        logger.info(
            "Role [%s] completed — %d tokens",
            self.role_name,
            usage.total_tokens if usage else 0,
        )

        # Determine suggested next role
        next_role = self.suggested_next

        # Build execution record for DB persistence (engine will save it)
        execution_record = {
            "role_name": self.role_name,
            "role_title": self.role_title,
            "role_description": self.role_description,
            "system_prompt": effective_prompt,
            "input_brief": input_brief,
            "output_deliverable": assistant_content,
            "suggested_next_role": next_role,
            "model_used": model,
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "total_tokens": usage.total_tokens if usage else None,
            "messages": [
                {"role": "system", "content": effective_prompt, "ordinal": 0},
                {"role": "user", "content": input_brief, "ordinal": 1},
                {"role": "assistant", "content": assistant_content, "ordinal": 2},
            ],
        }

        # Append to execution log in context (engine persists these)
        executions = context.get("_executions", [])
        executions.append(execution_record)

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
