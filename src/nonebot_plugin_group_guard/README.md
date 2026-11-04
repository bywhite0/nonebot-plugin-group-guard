# 群管整合插件

包名：`nonebot_plugin_group_guard`，面向 NoneBot2 / OneBot v11 的独立插件。

- 安装 wheel 后通过 `nonebot.load_plugin("nonebot_plugin_group_guard")` 加载；也可复制本目录到任意插件目录加载，二者不可重复加载。
- 默认数据在应用启动目录的 `data/group_guard/`；通过 `GROUP_GUARD_CONFIG_FILE`、`GROUP_GUARD_STATE_FILE` 指定绝对路径。
- 新配置默认关闭，不管理任何群；`/群管帮助` 查看管理命令。
- 审批支持兼容关键词、固定/多答案规则、人工模式；来源分类为可选显式映射，人机验证独立配置。
- 来源图保留 KhBot 水印，按局部背景亮度切换深浅字色，并使用细反色描边。
- 切换前先停其它实例相同群管功能；不分发运行配置、成员数据、密钥或第三方字体/背景。

完整安装、配置、命令和迁移说明在项目根目录 `README.md`；来源与许可见 `NOTICE.md`、`LICENSE`、`licenses/`。

