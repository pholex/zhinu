"""模型切换原因的展示名与故障属性，在线提示和离线诊断共用。"""

REASONS = {
    "manual": ("手动或宿主切换", False),
    "automatic": ("自动切换（具体原因未记录）", False),
    "preferred_recovered": ("首选模型恢复", False),
    "rate_limit": ("限流", True),
    "transient": ("暂时性服务或网络错误", True),
    "auth": ("鉴权失败", True),
    "quota": ("额度耗尽", True),
    "empty": ("连续空回复（未生成 token）", True),
}


def describe(reason: str) -> str:
    return REASONS.get(reason, (f"未知原因（{reason}）" if reason else "原因未记录", False))[0]


def is_failure(reason: str) -> bool:
    return REASONS.get(reason, ("", False))[1]
