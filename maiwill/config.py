"""MaiWill 插件配置模型与 WebUI 展示元数据。"""

import ipaddress
import re
from typing import Any, Literal

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator, model_validator


class PluginSectionConfig(PluginConfigBase):
    """插件启停和内部配置版本。"""

    __ui_label__ = "插件"
    __ui_icon__ = "puzzle"
    __ui_order__ = -1

    enabled: bool = Field(default=True, description="是否启用 MaiWill。关闭后不会运行意志监督任务，但会保留已保存的会话和体力账本。", json_schema_extra={"label": "启用 MaiWill", "hint": "关闭插件不会删除会话、事件或体力数据。", "order": 0})
    config_version: str = Field(default="0.1.35", description="插件配置结构的内部版本号。", json_schema_extra={"label": "配置版本（内部）", "hidden": True, "disabled": True, "order": 99})


class GeneralConfig(PluginConfigBase):
    """配置意志模型、断连恢复、退避和休息上限。"""

    __ui_label__ = "意志会话"
    __ui_icon__ = "brain"
    __ui_order__ = 0

    model_task: Literal[
        "planner", "replyer", "utils", "memory", "mid_memory",
        "learner", "fast_model", "emoji", "vlm",
    ] = Field(
        default="planner",
        description="意志会话沿用的 MaiBot 模型任务，模型列表和任务参数由宿主管理。",
        json_schema_extra={
            "label": "沿用模型任务",
            "x-widget": "select",
            "hint": "planner 规划、replyer 回复、utils 通用、memory 记忆、mid_memory 回想、learner 学习、fast_model 快速、emoji 表情、vlm 视觉。请先在 MaiBot 模型管理中配置所选任务；宿主已有的继用规则照常生效。",
        },
    )
    model_name: str = Field(
        default="",
        description="可选：仅为 MaiWill 的请求指定一个宿主已配置的模型。",
        json_schema_extra={
            "label": "指定模型名称（可选）",
            "hint": "留空时完整沿用上方任务的模型选择；填写时使用 MaiBot 模型管理中的模型名称（不是服务商模型标识符），其余任务参数仍沿用上方任务。模型不可用时会报告错误，不自动改用其他模型。",
        },
    )
    reconnect_initial_seconds: int = Field(default=5, ge=1, le=3600, description="意志会话断连后，第一次重新唤起前等待的时间。", json_schema_extra={"label": "首次重连等待（秒）", "hint": "默认 5 秒；连续失败时会按退避倍数逐步增加。"})
    reconnect_multiplier: float = Field(default=2.0, gt=1, le=10, description="连续断连时，下一次重连等待时间相对于上一次的倍数。", json_schema_extra={"label": "重连退避倍数", "hint": "例如 2 表示每次连续失败后等待时间翻倍。"})
    reconnect_max_seconds: int = Field(default=600, ge=5, le=86400, description="断连重连等待时间允许达到的最大值。", json_schema_extra={"label": "最大重连等待（秒）", "hint": "默认 600 秒，也就是 10 分钟；达到后继续按此间隔尝试。"})
    stable_window_seconds: int = Field(default=60, ge=1, le=86400, description="会话连续稳定运行达到该时长后，清零连续断连次数。", json_schema_extra={"label": "稳定运行窗口（秒）", "hint": "默认 60 秒；单次模型请求成功不会立即清零退避。"})
    rest_max_seconds: int = Field(default=43200, ge=1, le=43200, description="单次休息请求允许的最长时长。", json_schema_extra={"label": "最长休息时间（秒）", "hint": "硬上限为 43200 秒，即 12 小时。"})
    idle_wait_seconds: int = Field(default=3600, ge=60, le=43200, description="无停止请求时模型错误地结束本轮后，代码安排的自主恢复等待时长。", json_schema_extra={"label": "自主恢复等待（秒）", "hint": "正常情况下麦麦会用 rest 自己选择休息时长；只有无停止请求时错误地选择 finish，代码才按此等待后继续探索。无效决策三次纠正失败会按断连退避重试。默认 3600 秒（1 小时）。"})
    context_limit_tokens: int = Field(default=258000, ge=1024, le=2000000, description="MaiWill 整理意志会话历史时使用的上下文 token 目标上限。", json_schema_extra={"label": "上下文管理上限（token）", "hint": "默认 258,000。达到下方阈值后裁剪较早历史，并目标保留约此值一半的近期记录；这是 MaiWill 的软管理目标，不会覆盖模型供应商的上下文设置。"})
    context_compression_threshold: float = Field(default=0.8, gt=0, le=1, description="完整请求预估或宿主报告的 prompt 用量达到上下文管理上限的此比例时，整理较早会话历史。", json_schema_extra={"label": "上下文压缩阈值", "hint": "默认 0.8（80%）。例如上限 258,000 时，预估或实测达到约 206,400 token 开始整理，然后目标保留最近约 129,000 token 的完整历史。"})
    memory_bridge_enabled: bool = Field(default=False, description="启用个人用途的本机 MaiBot WebUI 记忆读写桥接。插件从 MAIBOT_INSTANCE_DIR 实例的 data/webui.json 读取 WebUI Token；此凭据具有 WebUI 管理员权限。", json_schema_extra={"label": "启用本机记忆桥接", "hint": "默认关闭。开启等于授予 MaiWill WebUI 管理员权限，只建议个人单机部署。开启后可检索/读取 MaiWill 自己写入的共享记忆，并将已整理候选写入 A_Memorix。凭据只用于本机回环请求，不保存到插件配置或数据库。"})
    memory_webui_url: str = Field(default="http://127.0.0.1:8001", description="本机 MaiBot WebUI 地址；只允许 localhost 或回环 IP。", json_schema_extra={"label": "本机 WebUI 地址", "hint": "默认 http://127.0.0.1:8001。桥接会拒绝远程地址，避免把管理员 Token 发往网络上的其他主机。"})

    @field_validator("model_name")
    @classmethod
    def normalize_model_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("memory_webui_url")
    @classmethod
    def validate_memory_webui_url(cls, value: str) -> str:
        from .memory_bridge import validate_loopback_url
        return validate_loopback_url(value)

    @model_validator(mode="after")
    def validate_reconnect_window(self) -> "GeneralConfig":
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            raise ValueError("断连重试最大等待时间不能小于首次等待时间")
        return self


class ResourceConfig(PluginConfigBase):
    """自主模型调用使用的体力池与恢复速度。"""

    __ui_label__ = "自主活动体力"
    __ui_icon__ = "battery"
    __ui_order__ = 1

    capacity_tokens: int = Field(default=10000000, ge=1, le=100000000, description="体力池最多可以储存的 token 等值体力。", json_schema_extra={"label": "体力上限（token）", "hint": "这是储量上限，不是每日消费上限；默认 10,000,000。"})
    recovery_tokens_per_hour: float = Field(default=1000000.0, ge=0, le=100000000, description="体力按实际经过时间恢复的速度。", json_schema_extra={"label": "每小时恢复体力", "hint": "插件停用或 MaiBot 关机期间也会按经过时间计算恢复；默认每小时 1,000,000。"})
    start_at_capacity: bool = Field(default=True, description="第一次创建体力账本时是否直接从体力上限开始。", json_schema_extra={"label": "首次启用时充满体力", "hint": "只影响首次创建账本；修改配置不会自动补满已有账本。"})


class PhoneConfig(PluginConfigBase):
    """配置 MaiWill 唯一允许连接的专属 Android 设备。"""

    __ui_label__ = "专属手机"
    __ui_icon__ = "smartphone"
    __ui_order__ = 2

    adb_path: str = Field(default="", description="ADB 可执行文件的本地完整路径。", json_schema_extra={"label": "ADB 程序路径", "hint": "填写专属 Android 模拟器或设备配套的 adb.exe 完整路径；留空时手机能力保持未配置。"})
    device_address: str = Field(default="127.0.0.1:16384", description="MaiWill 专属 Android 设备的 IP 和 ADB 端口。", json_schema_extra={"label": "专属设备 IP:端口", "hint": "默认 MuMu 地址 127.0.0.1:16384。插件始终只向此地址发送 ADB 命令，不需要填写序列号。"})

    @field_validator("adb_path", mode="before")
    @classmethod
    def normalize_adb_path(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("device_address", mode="before")
    @classmethod
    def validate_device_address(cls, value: Any) -> str:
        address = str(value or "").strip()
        try:
            host, port_text = address.rsplit(":", 1)
            if not re.fullmatch(r"[0-9]{1,5}", port_text):
                raise ValueError
            if ":" in host and not (host.startswith("[") and host.endswith("]")):
                raise ValueError
            ip = ipaddress.ip_address(host[1:-1] if host.startswith("[") and host.endswith("]") else host)
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise ValueError("设备地址必须是有效的 IP:端口，例如 127.0.0.1:16384（端口 1–65535）") from None
        return f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"


class MaiWillConfig(PluginConfigBase):
    """MaiWill 主配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    resources: ResourceConfig = Field(default_factory=ResourceConfig)
    phone: PhoneConfig = Field(default_factory=PhoneConfig)
