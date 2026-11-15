"""Grounded assistant public surface (AS.01 LLM client + AS.03 read-only tools + AS.04 chat).

Purpose: Export the LLM client foundation (protocol, models, errors), the
read-only assistant tools/schemas for FR-AS-02, the chat service for
FR-AS-01/03, and assistant availability (AS.05).
Inputs: Tool arguments, narrow read dependencies, chat messages/context.
Outputs: Provider-neutral LLM types, assistant-facing results, streaming chat.
Called by: assistant service/adapters, API endpoint, tests (AS.01/AS.03/AS.04/AS.05).
"""

from orbitalis.assistant.availability import assistant_readiness, is_assistant_configured
from orbitalis.assistant.knowledge import KnowledgePackMissingError
from orbitalis.assistant.limits import (
    AssistantDailyBudgetError,
    AssistantLimiter,
    AssistantRateLimitError,
)
from orbitalis.assistant.llm import (
    AssistantLLMError,
    LLMClient,
    LLMMessage,
    LLMProviderError,
    LLMProviderUnavailableError,
    LLMStreamChunk,
    LLMTimeoutError,
    LLMToolCall,
)
from orbitalis.assistant.schemas import (
    CandidateView,
    ChatContext,
    ChatDoneEvent,
    ChatErrorEvent,
    ChatEvent,
    ChatMessage,
    ChatRequest,
    ChatTokenEvent,
    ChatToolCallEvent,
    ExperimentDetailResult,
    ExperimentListResult,
    ExplainFlagsResult,
    ListCandidatesResult,
)
from orbitalis.assistant.service import AssistantService
from orbitalis.assistant.tools import (
    explain_flags,
    list_candidates,
    read_experiment_results,
)

__all__ = [
    "AssistantDailyBudgetError",
    "AssistantLimiter",
    "AssistantLLMError",
    "AssistantRateLimitError",
    "AssistantService",
    "assistant_readiness",
    "CandidateView",
    "ChatContext",
    "ChatDoneEvent",
    "ChatErrorEvent",
    "ChatEvent",
    "ChatMessage",
    "ChatRequest",
    "ChatToolCallEvent",
    "ChatTokenEvent",
    "ExperimentDetailResult",
    "ExperimentListResult",
    "ExplainFlagsResult",
    "KnowledgePackMissingError",
    "ListCandidatesResult",
    "LLMClient",
    "LLMMessage",
    "LLMProviderError",
    "LLMProviderUnavailableError",
    "LLMStreamChunk",
    "LLMTimeoutError",
    "LLMToolCall",
    "explain_flags",
    "is_assistant_configured",
    "list_candidates",
    "read_experiment_results",
]
