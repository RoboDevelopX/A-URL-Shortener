"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.models.base import Base

__all__ = ["Base"]
