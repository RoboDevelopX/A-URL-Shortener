"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.models.base import Base
from app.models.link import Link, LinkStatus

__all__ = ["Base", "Link", "LinkStatus"]
