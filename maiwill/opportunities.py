"""从已有资料和启用能力构造可执行的自主探索入口。"""

from __future__ import annotations

from typing import Any


def build_exploration_options(
    *,
    available_capabilities: list[dict[str, Any]],
    learning_candidates: list[dict[str, Any]],
    recent_interests: list[dict[str, Any]],
    exploration_goals: list[dict[str, Any]],
    recent_actions: list[dict[str, Any]],
    context_message_count: int,
) -> list[dict[str, Any]]:
    """返回小而具体的入口清单，不创造资料或推测外部事实。"""
    capability_names = {item.get("name") for item in available_capabilities}
    options: list[dict[str, Any]] = []

    active_goals = [goal for goal in exploration_goals if goal.get("status") == "active"]
    if active_goals:
        goal = active_goals[0]
        options.append({
            "kind": "continue_goal",
            "title": str(goal.get("title", "当前探索目标"))[:200],
            "source_ref": str(goal.get("goal_id", ""))[:200],
            "next_step": str(goal.get("next_step", ""))[:500],
            "is_stalled": bool(goal.get("is_stalled")),
        })
    else:
        for goal in exploration_goals:
            if goal.get("status") == "deferred":
                options.append({
                    "kind": "resume_deferred_goal",
                    "title": str(goal.get("title", "暂缓目标"))[:200],
                    "source_ref": str(goal.get("goal_id", ""))[:200],
                    "next_step": str(goal.get("next_step", ""))[:500],
                })
                break

    phone_enabled = "phone.use" in capability_names
    recent_phone_observation = any(
        action.get("capability") == "phone.use"
        and (
            action.get("action_kind") in {"status", "screen", "screenshot"}
            or (
                isinstance(action.get("arguments"), dict)
                and action["arguments"].get("action") in {"status", "screen", "screenshot"}
            )
        )
        for action in recent_actions
    )
    if phone_enabled and not active_goals:
        options.append({
            "kind": "self_directed_discovery",
            "title": "没有现成目标时，自主发现下一件值得做的事",
            "available_capabilities": ["phone.use"],
            "next_step": (
                "没有活动目标或预先给定的来源，不代表无事可做。查看专属设备当前可见内容；"
                + ("近期观察仍可作为线索，从真实可见的页面继续一个只读或可恢复的浏览步骤。" if recent_phone_observation else "可从 status、screen 或 screenshot 安全观察开始。")
                + "从真实可见内容里自己挑一个有趣、具体的小问题，用一两步只读或可恢复操作了解更多；"
                "单步发现无需先建目标，只有值得持续调查时才引用成功行动创建目标。"
                "只浏览，不发送消息、不提交表单、不购买、不删除、不改设置、不运行 shell/ADB 命令，"
                "也不读取或暴露私人凭据。完成一步后重新判断是否值得继续，避免重复旧观察；"
                "没有具体、安全方向或体力需要恢复时再休息，不要为了保持忙碌而编造任务。"
            ),
        })

    candidates = [
        candidate for candidate in learning_candidates
        if candidate.get("status") in {"pending", "researching"}
    ]
    if candidates and "reflection.list" in capability_names:
        options.append({
            "kind": "review_learning_candidates",
            "title": "整理尚未完成的本地学习候选",
            "items": [
                {"external_id": str(item.get("external_id", ""))[:200],
                 "title": str(item.get("title", ""))[:160],
                 "status": item.get("status")}
                for item in candidates[:5]
            ],
            "next_step": "从候选中挑一个仍有价值、且当前能力能推进的主题；不要声称已同步到共享记忆。",
        })

    if recent_interests:
        options.append({
            "kind": "follow_recorded_interest",
            "title": "继续处理麦麦明确记录过的兴趣",
            "items": [
                {"external_id": str(item.get("external_id", ""))[:200],
                 "topic": str(item.get("topic", ""))[:200],
                 "reason": str(item.get("reason", ""))[:300]}
                for item in recent_interests[-5:]
            ],
            "next_step": "只围绕这些明确记录的兴趣继续；memory.search 仅在已启用且引用对应 interest_id 时可用。",
        })

    if phone_enabled and active_goals and not recent_phone_observation:
        options.append({
            "kind": "discover_configured_phone",
            "title": "观察已配置的专属手机，寻找真实可推进的主题",
            "next_step": "从 phone.use(action=status) 或 phone.use(action=screen) 只读观察开始；不要为制造任务而点击、输入或运行 shell。若观察到值得继续的主题，用成功观察的 action_id 建立目标。",
        })

    if recent_actions:
        options.append({
            "kind": "build_on_recent_action",
            "title": "从近期成功行动继续",
            "items": [
                {"action_id": str(item.get("action_id", ""))[:200],
                 "capability": str(item.get("capability", ""))[:100],
                 "summary": str(item.get("result_summary", ""))[:240]}
                for item in recent_actions[:5]
            ],
            "next_step": "检查行动结果是否留下值得继续的问题；只有真实成功结果可作为目标依据。",
        })

    if context_message_count > 0 and "diary.write" in capability_names:
        options.append({
            "kind": "reflect_on_session",
            "title": "回看当前会话中的真实观察并做简短整理",
            "next_step": "只根据已保存会话中确实发生的观察、行动和结果写日记或学习候选；没有新内容时不要重复整理。",
        })

    if not phone_enabled:
        safe_discovery_capabilities = sorted(
            capability_names.intersection({"diary.write", "reflection.create", "reflection.list"})
        )
        if safe_discovery_capabilities and not active_goals:
            options.append({
                "kind": "self_directed_discovery",
                "title": "没有现成目标时，从已有资料发现可推进的小问题",
                "available_capabilities": safe_discovery_capabilities,
                "next_step": (
                    "结合已有日记、会话观察和学习候选，自己提出一个具体且有依据的小问题；"
                    "只整理或调查现有资料，不编造新观察。单步发现不要求先创建目标；"
                    "只有值得持续跟进时，才从真实成功行动创建目标。"
                ),
            })

    return options[:8]
