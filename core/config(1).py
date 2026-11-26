# config.py
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path


class ConfigNode:
    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] miss key: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] not a dict"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        return MappingProxyType(self._data)

    def save_config(self) -> None:

        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() only support AstrBotConfig"
            )
        self._data.save_config()


class AutoBoxConfig(ConfigNode):
    enter: bool
    exit: bool


class GroupReviewConfig(ConfigNode):
    enable: bool
    blacklist: list[str]


class AntiFloodConfig(ConfigNode):
    enable: bool
    max_count: int
    window_seconds: int
    ban_seconds: int
    max_chars: int
    text_ban_seconds: int


DEFAULT_DISPLAY_OPTIONS = [
    "QQ号",
    "UID",
    "QID",
    "昵称",
    "备注",
    "群昵称",
    "群头衔",
    "群身份",
    "性别",
    "生日",
    "星座",
    "生肖",
    "年龄",
    "血型",
    "电话",
    "邮箱",
    "家乡",
    "现居",
    "地区",
    "学校",
    "职位",
    "职业",
    "个性标签",
    "风险账号",
    "机器人账号",
    "隐藏QQ等级",
    "特权图标",
    "屏蔽用户",
    "免打扰",
    "特别关心",
    "空间特别关心",
    "自定义状态",
    "企点企业",
    "QQVIP",
    "年VIP",
    "VIP等级",
    "群等级",
    "QQ等级",
    "加群时间",
    "最后发言",
    "注册时间",
    "登录天数",
    "签名",
]


class PluginConfig(ConfigNode):
    protect_ids: list[str]
    autobox: AutoBoxConfig
    group_review: GroupReviewConfig
    antiflood: AntiFloodConfig
    recall_time: int

    _plugin_name: str = "astrbot_plugin_infocard"

    def __init__(self, cfg: AstrBotConfig, context: Context):
        super().__init__(cfg)
        self.context = context
        self.admins_id: list[str] = context.get_config().get("admins_id", [])
        self.temp_dir = Path(get_astrbot_temp_path()) / self._plugin_name / "cards"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._normalize_protect_ids()

    def _normalize_protect_ids(self):
        if not self.admins_id:
            return
        for admin_id in self.admins_id:
            if admin_id not in self.protect_ids:
                self.protect_ids.append(admin_id)
        self.save_config()

    @property
    def display_options(self) -> list[str]:
        """信息显示选项固定为默认全量，不可配置更改"""
        return list(DEFAULT_DISPLAY_OPTIONS)
