from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.mycoderbench.host import RecordingOutlet, RuntimeTrialHost
from mycoder.config.mycoder import mycoderConfig
from mycoder.config.schema import Config
from mycoder.providers.base import LLMResponse
from mycoder.spine import ChatType, Origin, Source, TurnRequest
from mycoder.spine.events import Text


class _ScriptedProvider:
    def get_default_model(self) -> str:
        return "scripted/mycoderbench"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        return LLMResponse(
            content="mycoderBENCH_RUNTIME_OK",
            finish_reason="stop",
            usage={
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            },
        )


@pytest.mark.asyncio
async def test_mycoderbench_host_runs_the_real_runtime_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config = Config(
        agents={
            "defaults": {
                "workspace": str(workspace),
                "model": "scripted/mycoderbench",
                "max_tool_iterations": 2,
            }
        }
    )
    mycoder_config = mycoderConfig(
        memory={"backend": None},
        plugins={"disabled": []},
        skill_forge={"router": {"enabled": False}},
    )
    outlet = RecordingOutlet("bench")
    host = await RuntimeTrialHost.build(
        config=config,
        mycoder_config=mycoder_config,
        provider=_ScriptedProvider(),
        cron_service=None,
        outlet=outlet,
    )
    request = TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel="bench",
            chat_id="smoke",
            sender_id="user",
            chat_type=ChatType.DM,
        ),
        text="Return the smoke marker.",
        conversation="bench:smoke",
    )

    observation = await host.run(request)
    await host.close()

    session = host.assembly.session_manager.get_or_create("bench:smoke")
    assert observation.runtime_state.value == "completed"
    assert observation.delivery_state.value == "delivered"
    assert [event.content for event in outlet.events if isinstance(event, Text)] == ["mycoderBENCH_RUNTIME_OK"]
    assert any(message.get("content") == "Return the smoke marker." for message in session.messages)
