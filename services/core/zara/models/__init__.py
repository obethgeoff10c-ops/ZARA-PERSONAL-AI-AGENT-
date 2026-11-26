"""Import every model so Alembic and the mapper registry see the full schema."""

from __future__ import annotations

from zara.models.agent import (
    AgentRun,
    Approval,
    Conversation,
    Message,
    RunEvent,
    ToolExecution,
)
from zara.models.audit import AuditLog
from zara.models.core import AuthSession, FeatureFlag, Tenant, User, UserPreference, UserProfile
from zara.models.crm import Contact
from zara.models.graph import Entity, EntityMention, EntityRelationship
from zara.models.integrations import (
    Integration,
    OAuthCredential,
    OAuthState,
    SyncCursor,
    WebhookSubscription,
)
from zara.models.memory import (
    BlockedFact,
    MemoryEmbedding,
    MemoryItem,
    MemoryRevision,
    MemorySource,
    RetrievalLog,
)
from zara.models.proactive import Insight, Notification
from zara.models.work import Goal, Project, Task

__all__ = [
    "AgentRun",
    "Approval",
    "AuditLog",
    "AuthSession",
    "BlockedFact",
    "Contact",
    "Conversation",
    "Entity",
    "EntityMention",
    "EntityRelationship",
    "FeatureFlag",
    "Goal",
    "Insight",
    "Integration",
    "MemoryEmbedding",
    "MemoryItem",
    "MemoryRevision",
    "MemorySource",
    "Message",
    "Notification",
    "OAuthCredential",
    "OAuthState",
    "Project",
    "RetrievalLog",
    "RunEvent",
    "SyncCursor",
    "Task",
    "Tenant",
    "ToolExecution",
    "User",
    "UserPreference",
    "UserProfile",
    "WebhookSubscription",
]
