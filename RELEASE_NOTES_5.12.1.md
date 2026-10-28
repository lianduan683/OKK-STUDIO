# OKK STUDIO 5.12.1 · AMD / Intel / RTX 4050 适配

- 新增 AMD AMF、Intel QSV 编码选项，保留 NVIDIA NVENC，覆盖 H.264 / HEVC / AV1（按实际硬件能力）。
- 检测从 FFmpeg 编译列表改为短真实编码与输出校验，不可用编码器不显示；10-bit 能力独立校验。
- 1080p 代理、单文件、拼接、多机位与透明图层导出使用厂商对应参数，避免 NVENC 参数误传 AMF/QSV。
- 移除 Windows 监看固定 NVIDIA 的限制，D3D11/D3D11VA 支持多厂商。
- 纳入 RTX 4050 / RTX 4050 Laptop 识别，使用单编码引擎配置和保守自动并发。

本机 AMD Radeon 890M 和 RTX 5090 Laptop 已验证 AMF/NVENC 编码初始化与 HEVC/AV1 10-bit 输出；AMF 多机位切点、连续音频、10-bit 透明叠加已验证。Intel QSV 和 RTX 4050 完成模拟能力、参数和回退测试，尚未在这些硬件上实测。

AMD/Intel 的复杂画面处理使用兼容 CPU 滤镜，硬件负责 AMF/QSV 编码。透明图层预览仍是最高 720p/30fps 的 CPU 合成，复杂工程可能降低刷新率。

这是源码更新版，不包含新打包 EXE、大型运行库或完整特效视频素材。保留已有 bin/、assets/ 与用户工程，覆盖源码后运行“启动OKK STUDIO.bat”，标题应显示 5.12.1。首次运行依赖配置见 README。

技术参考：[FFmpeg QSV 编码文档](https://ffmpeg.org/ffmpeg-codecs.html#QSV-Encoders)、[AMD AMF 硬件支持](https://github.com/GPUOpen-LibrariesAndSDKs/AMF/wiki/GPU-and-APU-HW-Features-and-Support)、[NVIDIA 编解码支持矩阵](https://developer.nvidia.com/video-encode-decode-support-matrix)。
