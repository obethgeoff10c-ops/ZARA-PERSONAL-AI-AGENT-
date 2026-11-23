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
    SyncCursor,
    WebhookSubscription,
)
from zara.models.memory import (
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
