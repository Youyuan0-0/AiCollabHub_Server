"""外部计量预算的展示计算，不保存未经确认的整页快照。"""

from app.schemas.overview import Budget, BudgetInput


def budget_summary(data: BudgetInput, *, available: bool) -> Budget:
    """按已用金额与限额计算预算，零限额避免除零，超支不裁剪使用率。"""
    percentage = round(data.used / data.limit * 100, 2) if data.limit else 0
    if not available:
        message = "暂无预算数据"
    elif data.limit == 0:
        message = "预算额度为零，已有支出" if data.used else "尚未设置预算额度"
    elif data.used > data.limit:
        message = "当前消耗已超出预算"
    elif percentage >= data.warning_threshold:
        message = "当前消耗已达到预算预警阈值"
    else:
        message = "当前消耗在预算范围内"
    return Budget(**data.model_dump(), percentage=percentage, message=message)
