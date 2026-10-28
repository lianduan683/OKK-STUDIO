# OKK STUDIO 5.12.0

Windows 多机位剪辑与视频导出工具。保留 OKK STUDIO 原有深色工作区，支持媒体库、时间线编辑、调色、转场、字幕、透明图层与 NVENC 导出。

本次版本：**5.12.0**，标签：**v5.12.0**。

## 5.12.0 更新

- **1080p 代理**：监看上方的一键代理从完整原片生成，保持比例、不超过 1920×1080，最高 30fps，优先 CUDA/NVENC；任务在后台逐个处理，完成后自动接入。
- **链接已有代理**：支持为所选素材选择代理文件，或从文件夹按原片名批量匹配。校验视频与完整时长，允许代理帧率不同；同名冲突或多个候选需手动选择。支持已有 MP4、MOV、MKV、MXF 等视频代理，不重复转码。
- **最多 36 机位**：监看网格可以分别设置 1–6 行、1–6 列，默认 3×3，最大 6×6；布局偏好自动保存。
- **透明图层**：时间线支持带 Alpha 的视频堆叠，上方轨道显示在画面上层；支持拖动轨道标签排序、隐藏、撤销与工程保存。
- **可组合面板**：面板可浮动、靠边排列、合并成标签组，整组标签也能一起拖出。修复组合变灰和浮动监看重复刷新引起的性能下降。
- **时间线与预览**：修复素材拖入显示，支持鼠标拖动节目切点；左右键单次精确回帧、长按连续采样，松开准确定位。提供释放监看缓存入口。
- **检查器与特效库**：调色、转场、防抖等常用功能按模块提供，特效库可直接从顶部或检查器进入。

## 获取与启动

这是**源码更新版本**，不包含打包完成的新 EXE、大型外部运行库或完整特效素材包。

### 已有完整安装

1. 保存工程并关闭旧窗口。
2. 把本版本源码覆盖到已有的 OKK STUDIO 文件夹，保留原有 `bin/`、`assets/` 与用户工程。
3. 双击 `启动OKK STUDIO.bat`，确认标题显示 **5.12.0**。旧 EXE 不会因替换 Python 源码自动升级。

### 从源码配置

- Windows 10/11，Python 3.11 或更高版本；本机验证使用 Python 3.13。
- 安装 Python 依赖：`python -m pip install -r requirements-build.txt`。该文件也包含可选打包工具 PyInstaller。
- 将兼容的 `ffmpeg.exe`、`ffprobe.exe` 放在 `bin/`，或加入系统 PATH；多机位监看还需要 `mpv.exe`。
- GPU 实时监看需要 `bin/gpu_live/libmpv-2.dll`，并保留该目录里的着色器、布局与许可证文件。来源和校验信息见 `bin/gpu_live/runtime_sources.json`。
- 可选 GPU 导出运行库放在 `bin/gpu_export/`，来源信息见该目录的 `runtime_sources.json`。Gyroflow、ExifTool 属于可选外部组件。
- 最后运行 `启动OKK STUDIO.bat` 或 `python rtx5090_exporter.py`。

外部工具与 NVIDIA 驱动依各自许可证使用；GPU 功能会依据本机实际能力检测并选择兼容路径。

## 代理、透明预览与导出

代理用于减轻监看解码负担，**最终导出读取原片及对应的原分辨率效果缓存**。代理需要来自同一完整原片，不能裁切或变速；时长校验不能替代人工确认素材身份。

透明图层预览目前使用 CPU 连续合成，最高 **720p/30fps**；这与生成的 **1080p 代理**是两个不同设置，不限制最终导出的分辨率。确认不透明的图层可使用代理，带 Alpha 的图层保留透明原片，避免 H.264 代理丢失透明区域。复杂叠加可能降低刷新率；拖动和长按时跳过中间采样，松开后准确停在目标帧。

## 特效资源

完整安装的特效素材位于 `assets/effects_library/`，约 **5.5 GB**。本源码仓库只附资源清单，**不包含完整视频叠加素材与转场遮罩文件**，也没有自动下载器。已有安装请保留该目录；缺少对应文件时软件会跳过这些库素材。337 项是完整资源安装时的库数量，不是仅下载源码后的可用数量。

## 验证范围

本机使用独立短合成素材验证了 1080p 代理生成与缓存、已有 ProRes 代理校验、九机位停靠/浮动播放、透明图层合成与导出、逐帧与连续定位、工程保存恢复和资源回收。九机位测试的播放器时钟约 30fps，未报告解码/呈现丢帧；此结果不是对任意长工程、任意素材或 36 路满载的速度保证。

测试位于 `tests/`。部分测试需要 Windows Tk、FFmpeg/MPV 或真实 GPU，运行前请配置相应依赖。

详细操作说明见 [合并版使用说明.txt](合并版使用说明.txt)。历史中文说明见 [README_CN.md](README_CN.md)。

---

## 原项目说明与致谢

需要部署环境：python 3.11及以上版本（建议python 3.13以上），支持大部分RTX显卡（RTX 4050，RTX 4050 laptop除外），有bug不要压力作者喵，会尝试解决的喵

关注塔菲喵，关注塔菲谢谢喵   

好了不串了   

飞黄如星，霄洒凌雲，星辰大海，为雲而来   

玫瑰星云，永不凋零   

黄霄雲姐姐，我喜欢你（哭）

Thanks to the HeyPuter/everything project and its contributors for openly sharing the results and concepts behind their software development; these have served as an inspiration for the explorations at OKK STUDIO. We also appreciate your contributions to the open-source community.
