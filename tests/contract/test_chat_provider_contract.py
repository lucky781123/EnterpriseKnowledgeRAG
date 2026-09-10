import pytest

from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.protocols import ChatMessage, ChatRequest


@pytest.mark.asyncio
async def test_chat_provider_contract_supports_complete_stream_and_health_check():
    provider = FakeChatProvider(lambda request: "contract answer")
    request = ChatRequest(messages=[ChatMessage("user", "question")], model="fake")

    response = await provider.complete(request)
    chunks = [chunk async for chunk in provider.stream(request)]

    assert response.content == "contract answer"
    assert "".join(chunks).strip() == "contract answer"
    assert await provider.health_check() is True
