from sqlalchemy import CheckConstraint, Column, Integer

from ..core.db import Base


class SystemConfig(Base):
    __tablename__ = "system_config"

    id = Column(Integer, primary_key=True)
    max_upload_size_mb = Column(Integer, nullable=False)
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_system_config_singleton"),
        CheckConstraint("max_upload_size_mb > 0", name="ck_system_config_upload_positive"),
    )
