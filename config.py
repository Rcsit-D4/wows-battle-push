# -*- coding: utf-8 -*-
"""插件配置模型，对应 config.toml 的 [plugin] 节"""

from maibot_sdk import Field, PluginConfigBase


class PushSection(PluginConfigBase):
    __ui_label__ = "战舰世界战绩推送"

    config_version: str = Field(default="1.0.0", description="配置版本号")
    poll_interval_minutes: int = Field(default=5, ge=1, le=1440, description="轮询间隔（分钟）")
    enabled_battle_types: list[str] = Field(
        default=["pvp_solo", "rank_solo"],
        description="监控的对局类型：pvp_solo / rank_solo / pvp_div2 / pvp_div3 / pve",
    )
    push_enabled: bool = Field(default=True, description="是否自动推送新战绩")
    admin_qq: list[str] = Field(default=[], description="管理员QQ号白名单")
    log_retention_days: int = Field(default=30, ge=7, le=365, description="战斗日志保留天数")


class PluginConfig(PluginConfigBase):
    plugin: PushSection = Field(default_factory=PushSection)
