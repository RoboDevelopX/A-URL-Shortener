"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.models.api_key import ApiKey
from app.models.base import Base
from app.models.click import Click, DeviceType
from app.models.link import Link, LinkStatus

__all__ = ["ApiKey", "Base", "Click", "DeviceType", "Link", "LinkStatus"]
