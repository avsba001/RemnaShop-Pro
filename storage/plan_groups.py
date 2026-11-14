"""Plan-specific squad selection; NULL alone denotes a legacy default plan."""
import json
import re

_UUID = re.compile(r'^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}|00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff)$')


def validate_groups(groups):
    if not isinstance(groups, list) or not groups:
        raise ValueError('请至少选择一个内部组')
    result = []
    for value in groups:
        if not isinstance(value, str) or not _UUID.fullmatch(value):
            raise ValueError('内部组 UUID 格式无效')
        value = value.lower()
        if value not in result:
            result.append(value)
    return result


def plan_groups(plan, default=None):
    """Explicit corrupt/empty data must never silently inherit global access."""
    plan = dict(plan)
    raw = plan.get('group_uuids')
    if raw is not None:
        return validate_groups(json.loads(raw) if isinstance(raw, str) else raw)
    return [default] if default else []


def snapshot_groups(snapshot):
    if 'group_uuids' in snapshot and snapshot['group_uuids'] is not None:
        return plan_groups(snapshot)
    return [snapshot['group_uuid']] if snapshot.get('group_uuid') else []


def require_available(groups, inventory):
    available = {str(item['uuid']).lower() for item in inventory}
    if any(group.lower() not in available for group in groups):
        raise ValueError('所选内部组已不存在或不可访问，请管理员重新选择')
