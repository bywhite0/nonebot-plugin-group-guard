# 来源与许可

本目录是面向 NoneBot2 / OneBot v11 的群管整合实现，保留原插件功能与配置语义，并重写框架绑定、持久化、消息安全、图表与验证生命周期。

## 原项目

- **astrbot_plugin_joinmanager v1.5.1**：作者 清蒸云鸭（WhiteCloudOL）。
  - https://github.com/WhiteCloudOL/astrbot_plugin_joinmanager
  - 源实现 `main.py` 提供关键词分类审批、来源记录、通知与模板；`draw.py` 提供来源统计图的布局参考。
  - 原部署中的自定义水印由 bywhite0 添加，本实现保留原文并按背景亮度自动切换深浅色；不以水印代替源码许可和致谢。
  - 原项目声明灵感来自 https://github.com/qiqi55488/astrbot_plugin_appreview 。
  - 原许可证完整保存在 `licenses/astrbot_plugin_joinmanager.txt`。
- **astrbot_plugin_group_combined_captcha_verify v1.2.1**：本地元数据作者 bywhite0，源码注册信息署名 VanillaNahida、NyaNyagulugulu、白影White。
  - https://github.com/bywhite0/astrbot_plugin_group_combined_captcha_verify
  - 上游 https://github.com/VanillaNahida/astrbot_plugin_group_geetest_verify
  - 其 README 致谢 huntuo146 的数学验证 PRO，以及 yjwmidc / makotowu 的验证后端贡献。
  - 原许可证完整保存在 `licenses/astrbot_plugin_group_combined_captcha_verify.txt`。

两份本地原许可证均为 GNU Affero General Public License v3，保留各自文件原样；本目录 `LICENSE` 提供完整 AGPL v3 文本。
此说明只约束本整合插件及实际构成衍生/组合作品的边界，不据此无条件宣称宿主中所有无关插件被重新许可。

## 实现变化

- AstrBot 事件绑定换为 NoneBot 2.4.4 / OneBot v11 的 request/notice/message 与命令。
- 审批默认仅作用于配置范围，整合总开关默认关闭。
- 状态按机器人账号、群与成员隔离，持久化原子替换，超时检查防旧轮次误伤。
- 极验 HTTP client 使用已有 httpx；异常与错误验证码分开处理。
- 图表使用 Pillow，避免 matplotlib 依赖与共享输出文件竞争。
- 以标准 Python 包独立安装，数据路径独立于包安装位置；审批规则与可选来源分类分开。
- 文本、@ 和图片使用显式消息段，不把用户文本作为 CQ 代码执行。

未复制其它 NoneBot / Koishi 候选插件源码。

## 运行数据与素材

本机迁入的 API 密钥、群配置、成员记录、字体与背景不在本插件源码许可说明中被重新授权；不可因复制运行目录就视为可公开分发这些内容。
`data/group_guard/` 不是源码交付包的一部分。
