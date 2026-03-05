"""
SQLAlchemy model for the agent_logs table.
Centralised structured logging for all agents.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class AgentLog(Base):
    """Structured log entry from any agent in the system."""

    __tablename__ = "agent_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    log_level: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<AgentLog [{self.log_level}] {self.agent_name}: {self.message[:60]}>"
