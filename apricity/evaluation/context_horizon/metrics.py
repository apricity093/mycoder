"""Pure aggregation for Context Horizon local pilot rows."""

from __future__ import annotations

from typing import Any


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = sorted({row["task_id"] for row in rows})
    conditions = sorted({row["condition"] for row in rows})
    by_key = {(row["task_id"], row["condition"]): row for row in rows}

    task_rows = []
    for task_id in tasks:
        item = {"task_id": task_id}
        for condition in conditions:
            row = by_key.get((task_id, condition), {})
            item[f"{condition}_pressure_crossed"] = bool(row.get("pressure_crossed"))
            item[f"{condition}_no_safe_range"] = bool(row.get("no_safe_range"))
            item[f"{condition}_context_pass"] = bool(row.get("context", {}).get("context_pass"))
            item[f"{condition}_mechanics_pass"] = bool(row.get("mechanics", {}).get("mechanics_pass"))
        a_row = by_key.get((task_id, "A"), {})
        item["A_lost_fact_or_overflow"] = bool(a_row.get("no_safe_range")) or not bool(a_row.get("context", {}).get("context_pass"))
        task_rows.append(item)

    crossed_tasks = [item["task_id"] for item in task_rows if all(item.get(f"{condition}_pressure_crossed") for condition in ("C", "E"))]
    loss_tasks = [item["task_id"] for item in task_rows if item["A_lost_fact_or_overflow"]]
    return {
        "conditions": conditions,
        "tasks": task_rows,
        "pressure_crossed_tasks_C_E": crossed_tasks,
        "pressure_cross_rate_C_E": len(crossed_tasks) / len(tasks) if tasks else 0.0,
        "A_lost_fact_or_overflow_tasks": loss_tasks,
        "calibration_gate_pass": len(crossed_tasks) / len(tasks) >= 0.8 and len(loss_tasks) >= 3,
    }
