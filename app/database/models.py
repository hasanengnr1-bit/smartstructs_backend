from .db import Base
from sqlalchemy import (
    String,
    Column,
    JSON,
    DateTime,
    ForeignKey,
    func,
    Integer,
    Boolean,
    Index,
    CheckConstraint,
    DECIMAL,
    Date,
    BigInteger,
)
from sqlalchemy.dialects.postgresql import JSONB


class User(Base):
    __tablename__ = "users"

    email = Column(String(255), unique=True, nullable=False, primary_key=True)
    password = Column(String(255), nullable=True)
    name = Column(String(255))
    tokens = Column(Integer, default=0)
    is_verified = Column(Boolean, default=False)

    otp_code = Column(String, nullable=True)
    otp_expires_at = Column(DateTime, nullable=True)

    reset_otp_code = Column(String, nullable=True)
    reset_otp_expires_at = Column(DateTime, nullable=True)


class Project(Base):
    __tablename__ = "projects"

    id = Column(String(255), primary_key=True)
    project_name = Column(String(255), nullable=False)
    user_email = Column(String(255), ForeignKey("users.email"), nullable=False)
    filename = Column(String(255), nullable=False)
    start_time = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    task_id = Column(String(255), nullable=True)

    longitude = Column(DECIMAL(10, 7))  # type: ignore[var-annotated]
    latitude = Column(DECIMAL(9, 7))  # type: ignore[var-annotated]
    risk_category = Column(String(20), default="II")
    design_method = Column(String(20), default="ASD")
    design_code = Column(String(20), default="IBC 2024")
    site_class = Column(String(20), default="default")
    exposure_category = Column(String(5), default="B")

    hazard_data = Column(JSON(), default={})
    extracted_data = Column(
        JSON(),
        nullable=True,
    )
    analysis_data = Column(JSON(), nullable=True)
    status = Column(
        String(50),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_projects_email_start_time", "user_email", "start_time"),
        CheckConstraint(
            "status IN ('fail', 'pass', 'analysis_error', 'extraction_error', 'extracting', 'extracted')",
            name="check_valid_status",
        ),
        CheckConstraint(
            "risk_category IN ('I', 'II', 'III', 'IV')", name="check_valid_rc"
        ),
        CheckConstraint(
            "site_class IN ('Default', 'A', 'B', 'BC', 'C', 'CD', 'D', 'DE', 'E', 'F')",
            name="check_valid_sc",
        ),
        CheckConstraint("design_method IN ('ASD', 'LRFD')", name="check_valid_dm"),
        CheckConstraint("design_code IN ('IBC 2024','NDS')", name="check_valid_dc"),
        CheckConstraint(
            "exposure_category IN ('B', 'C', 'D')", name="check_valid_exposure"
        ),
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, index=True)
    user_email = Column(String(255), ForeignKey("users.email"))
    expires_at = Column(DateTime)
    revoked = Column(Boolean, default=False)


class OAuthExchangeCode(Base):
    __tablename__ = "oauth_exchange_codes"

    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, index=True, nullable=False)
    access_token = Column(String, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ProcessedOrder(Base):
    __tablename__ = "processed_orders"
    order_id = Column(String, primary_key=True)
    user_email = Column(String(255), ForeignKey("users.email"), nullable=False)
    processed_at = Column(DateTime, default=func.now())


class ChatHistory(Base):
    __tablename__ = "chat_histories"

    session_id = Column(String(255), primary_key=True)
    user_email = Column(String(255), ForeignKey("users.email"), nullable=False)
    messages = Column(JSONB, default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_chat_histories_user_email", "user_email"),)


class CalcSheet(Base):
    __tablename__ = "calc_sheets"

    calc_sheet_id = Column(String(64), primary_key=True)
    session_id = Column(
        String(255),
        ForeignKey("chat_histories.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_email = Column(String(255), ForeignKey("users.email"), nullable=False)
    # Raw tool-result data: {items, hazard, lateral}
    data = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_calc_sheets_session_id", "session_id"),
        Index("ix_calc_sheets_user_email", "user_email"),
    )


class TokenUsage(Base):
    __tablename__ = "token_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_email = Column(String(255), ForeignKey("users.email"), nullable=False)

    # Calendar month this record covers (first day of the month)
    period_start = Column(Date, nullable=False)

    # Cumulative token counts for the period
    input_tokens_used = Column(BigInteger, default=0, nullable=False)
    output_tokens_used = Column(BigInteger, default=0, nullable=False)

    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # One record per user per month — fast lookups
        Index("ix_token_usage_user_period", "user_email", "period_start", unique=True),
    )
