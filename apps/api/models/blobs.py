from sqlalchemy import BigInteger, Column, String

from .base import BaseModel


class Blob(BaseModel):
    __tablename__ = "blobs"

    sha256 = Column(String(64), nullable=False, unique=True, index=True)
    storage_key = Column(String(64), nullable=False, unique=True)
    content_type = Column(String(255), nullable=False)
    file_size = Column(BigInteger, nullable=False)
    original_filename = Column(String(1024), nullable=True)
