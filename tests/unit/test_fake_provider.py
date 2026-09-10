import pytest

from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.protocols import ChatMessage, ChatRequest


@pytest.mark.asyncio
async def test_fake_provider_is_deterministic():
    provider = FakeChatProvider(lambda request: "answer")
    request = ChatRequest(messages=[ChatMessage("user", "问题")], model="fake")
    response = await provider.complete(request)
    assert response.content == "answer"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_fake_provider_streams_without_exposing_reasoning():
    provider = FakeChatProvider(lambda request: "one two")
    request = ChatRequest(messages=[ChatMessage("user", "问题")], model="fake")
    chunks = [chunk async for chunk in provider.stream(request)]
    assert "".join(chunks).strip() == "one two"
