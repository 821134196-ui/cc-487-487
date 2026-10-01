# -*- coding: utf-8 -*-
"""业务异常类型。"""


class ServiceError(Exception):
    """业务规则冲突（400）。"""


class PermissionDenied(Exception):
    """角色无权操作（403）。"""


class VersionConflict(Exception):
    """库存乐观锁版本冲突（409）。"""


class NotFound(Exception):
    """资源不存在（404）。"""


class DuplicateEvent(Exception):
    """同一业务事件被重复确认（409）。"""
