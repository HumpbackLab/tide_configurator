# TideConfigurator

运行在电脑上的 TideSense 配置与标定网页，前端使用 Vite、React、TypeScript，后端使用 Python / FastAPI。
界面参考 Penguin Flight Console 的浅灰背景、白色直角面板、蓝色主操作和分区表单。
标定模块、网页、板型配置和运行数据位于本工程目录。设备端程序安装在板上，
TideConfigurator 不打包或上传录制器，运行时无需 SG2002 SDK。
本工程的 `calibration/` 是自带的标定实现；仓库根目录旧 `calibration/` 仍供原有脚本使用，
TideConfigurator 运行时不再加载它。

## 当前功能

- SSH 连接 SG2002 A53 TideSense，显示内核、可用空间及连接状态。
- `boards/<板型>/board.conf` 定义设备程序路径及板级参数；当前仅有 `tidesense`。
- 双目原始方向预览，板端 VPSS 硬件缩放及 VENC JPEG 编码，浏览器直连 HTTP/MJPEG。
- 网页直接生成并显示 AprilGrid，左侧大图、右侧测量和录制操作，支持全屏显示。
- 实测左右最外侧黑色小方块的外缘跨度，每段录制保存尺寸；标定使用独立生成的目标参数。
- 通过 Unix socket 开始、停止录制；停止后排空写队列并继续预览。
- 标定临时录制、指定路径的数据集录制、自动生成新路径的数据集录制。
- 取回完整录制，在录制记录中选择数据进行标定或删除。
- 广角 / 鱼眼两阶段标定，任务日志、取消、历史记录，以及配置和 PDF 报告下载。
- 上传 SchurVINS 板端发布包，保留已有标定配置并部署；启动、停止及查看 VIO 状态。

当前设备端接入 SG2002 双目，网页预览提供 640×400 / 640×480。标定板使用网页生成的 6×4 AprilGrid，无需下载，不指定纸张或显示尺寸。
IMU 模型固定为 `scale-misalignment`。RV1103 尚未接入。

## 安装与启动

需要 Linux、Python 3.10+、Node.js 22.12+（本机使用 24.16）、rootless Podman。
下面命令在 `tide_configurator` 目录执行：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd frontend
npm ci
npm run build
cd ..

# 准备标定环境，首次使用时执行
./calibration/environment/build.sh

./run.sh
```

浏览器打开 **http://127.0.0.1:8765**。生产运行只需要 Python 服务，Node.js 用于构建网页。
首次连接输入设备地址、用户名、密码；密码留空可使用本机 SSH 密钥或 Agent。
SSH 密码只用于本次连接，不写入配置文件。首次主机密钥保存在应用工作目录的 `known_hosts`，
不修改用户全局 SSH 配置，已记录的主机密钥变化时会拒绝连接。

启动预览时从板型配置读取设备端录制器路径，检查可执行后直接启动；
`/tmp/tide-configurator-*` 只保存本次控制 socket 和日志，不放程序。
不会自动停止 VIO 或其他相机/IMU 程序。启动前应释放相机和 IMU。
只有 SSH 连接时不启动采集；点击“启动预览”后才占用设备传感器。
正常断开时等待采集进程退出，保存最近的设备日志到本地 `device-last.log`，并清理本次 `/tmp` 部署目录。
启动失败也会保留日志并清理已退出的会话，保持 SSH 连接供重试；错误显示在启动按钮附近，全屏时同样可见。
会话以前台方式跟随 SSH 启动进程；后端或 SSH 异常退出时通过父进程退出信号正常收尾，释放相机和 IMU。

| 环境变量 | 默认值 / 作用 |
| --- | --- |
| `TIDE_PORT` | `8765`，本地 HTTP 端口 |
| `TIDE_WORKSPACE` | `tide_configurator/data`，本地录制缓存、任务、结果及主机密钥 |
| `KALIBR_IMAGE` | 使用本工程 `calibration/` 的配套镜像，默认 `localhost/tidesense-calibration:noetic` |

设备端录制器使用 `tide_cam` 的 SG2002 后端，构建时仍需要对应板卡 SDK。
先在设备上安装录制器，再通过网页连接。例如在 SDK 根目录运行
`scripts/dataset/build.sh`，将生成的 `install/soc_sg2002_tidesense_arm64_sd/tidesense-dataset/bin/tidesense_record`
安装到板上 `/usr/local/bin/tidesense_record` 并设置可执行权限。TideSense 当前已安装该程序。
VIO 部署使用板型目录下的启动、停止、状态和部署脚本，不要求电脑安装 SG2002 SDK。
数据和结果默认写入 `data/`，不再使用 SDK 的 `install/`。

板型配置位于 `boards/tidesense/board.conf`，其中 `recorder`、`vio`、`vio_config`、`vio_output` 是**设备上的绝对路径**；
`model`、`architecture`、`default_host`、`imu_name` 和 `data_dir` 也是板级设置。
增加板型时新建 `boards/<板型>/board.conf`，连接窗口会列出可用板型。
目前相机预览、录制和标定流程只对 TideSense 做过验证。

历史验证日志保存在忽略的 `artifacts/`，不参与运行，也不需要随软件交付。

## VIO 部署与运行

在“VIO 部署”页选择当前板型的 SchurVINS 发布包（`.tar` 或 `.tar.gz`），例如
`schurvins-gcc11-static-micro-ros-a53.tar`。发布包需包含与 `board.conf` 的 VIO 安装目录同名的顶层目录、
`run.sh`、AArch64 `bin/schurvins_stereo`、`schurvins.yaml` 和 `SHA256SUMS`。
网页先验证归档路径和程序架构，再通过 SSH 上传到板上暂存目录；板端核对 SHA-256、检查程序可执行，
随后替换安装目录。已有 `schurvins.yaml` / `tidesense.yaml` 会保留，旧程序留在带时间戳的备份目录。
VIO 正在运行时需先停止才能部署。

“启动 VIO”会关闭 TideConfigurator 的相机预览，使用 `vio_config` 启动设备上的 `vio`，
位姿输出及日志写入 `vio_output`。正在录制或取回数据时不能启动。停止按钮先发送 SIGINT，
必要时再发送 SIGTERM；断开网页或关闭后端不会自动停止已启动的 VIO。
TideSense 的 ISP 修复已消除 RGB CAC 与 TNR 配置警告，但当前内核仍会把其他 VI 消息
同步输出到 115200 波特串口，影响 SC7U22 的 SPI 读取时序。板级启动脚本仅在 VIO
运行期间把串口控制台日志级别降到 3；
内核消息仍保留在 `dmesg`。停止或进程异常退出时会自动恢复原级别。
网页显示的运行状态会定时刷新。设备端脚本保存在 `boards/tidesense/`，通过 SSH 执行，
不依赖 SDK 构建目录。部署程序与应用标定结果是两个操作；当前不会自动把新标定覆盖到设备配置。
VIO 页显示设备上 `schurvins.yaml` 的 SHA-256；标定记录显示各自配置文件的 SHA-256，
内容相同时标记“当前使用”。比较的是配置文件原始字节，外部修改文件后状态刷新会更新。

标定记录中的“应用到设备”只接受已完成的结果，且要求连接设备并停止 VIO。
后端校验本地配置后上传临时文件；设备校验 SHA-256、备份原配置、原子替换
`vio_config`，并更新安装包的 `SHA256SUMS`。原始标定结果不变，下载按钮仍可单独使用。

服务绑定本机回环地址，HTTP 和 WebSocket 拒绝外部网页 Origin。当前是单用户、单设备的本地工具。
前端开发时运行后端，再在 `tide_configurator/frontend` 执行 `npm run dev`，打开 Vite 输出的本地地址。
`/api` 和 WebSocket 会代理到 8765；后端改用其他端口时同步修改开发代理配置。

## 操作流程

1. 打开“相机标定”，按需先进入全屏，让设备面向左侧网页标定板。
2. 用尺量**左端黑色小方块的左外缘，到右端黑色小方块的右外缘**的水平距离，填写“实测跨度”（mm）。
   包含两端小方块，不包含白色留边，也不是两个小方块的中心距。
3. 连接设备并启动预览，选择广角 / 鱼眼、采集尺寸及录制时长。时长为 0 时手动停止。
4. 保持屏幕上的标定板位置和尺寸固定，移动整个设备录制，改变距离、位置和倾角并加入平移。
   页面不做在线标签检测、覆盖率或运动进度引导，双目原始预览位于右侧。
5. 停止后等待“正在保存”结束，再取回录制。板端排空队列并将本次录制文件刷盘后生成 COMPLETE；取回电脑时完整校验通过，才提供给标定。
6. 导入实测 IMU 噪声 YAML，核对“录制时标定板跨度”并开始标定。
   该值取自所选录制，不会跟随当前标定板的显示大小变化；可以修正尺量输入。
7. 在“标定记录”查看阶段、日志和结果。求解成功后下载 `schurvins.yaml` 和两份 PDF 报告。

标定使用 `/mnt/data/tide-configurator/calibration` 临时目录，下次标定录制前清理重建。
清理仅针对带有应用所有权标记的固定工作区，不跟随符号链接。取回电脑的数据使用独立目录保存，
可以继续用于旧任务。尚未取回且被下一次替换的临时录制会在历史中标记为“已替换”。
取回前核对设备文件中的独立录制标识，避免从被重建的临时目录误取另一段数据。

数据集录制从不清理已有目录：用户指定新路径，或留空自动生成带时间戳和唯一后缀的目录。
已有路径直接报错。录制器自身同样拒绝覆盖已有输出目录。

“数据录制”的记录列表提供“标定”入口，点击后会选中该份录制并进入标定设置；
尚未取回的录制需先取回电脑，填写录制时的标定板实测跨度并导入实测 IMU 噪声参数。
噪声参数模板中的 null 需替换为实测参数。
“删除”会移除记录、由 TideConfigurator 创建的电脑副本和缓存；设备端录制须连接对应设备，
且目录中的录制编号必须与记录一致才会删除。已导入的外部原始目录不会被删除；
运行中的录制、数据准备或标定任务结束前不能删除。
“标定记录”的“删除”会移除该任务的结果、报告和日志，保留原始录制及其准备缓存。

取回录制使用板端 HTTP 直传，SSH 只发送下载授权和释放命令。授权时核对录制 ID、完成标记和固定文件清单，并持有文件描述符；即使原目录被重建，也不会混入另一段录制。没有采集会话时自动启动不访问相机 / IMU 的文件服务。

网页显示 `HTTP 下载 → 校验 · SHA-256 · bag → 数据就绪`，包含字节数、速度、帧数和阶段百分比。
下载后一次遍历同时完成 checksum / 相机与 IMU 连续性检查、原始输入哈希和 ROS1 bag 写入。
已准备结果保存在工作区 `prepared/`，标定通过同一份 bag 和哈希启动；同文件系统使用硬链接，跨文件系统按进度复制。
复用前检查源文件大小、纳秒修改 / 状态变更时间、设备及 inode，以及处理代码和 bag 身份；输入变化会重新处理。
只复用成功的数据准备结果，不从失败的标定求解阶段继续。

校验失败的原始数据不会直接进入标定。相机压缩包单帧损坏时，可点击“剔除坏帧后另存副本”：
原始文件保持不变，新副本只复制通过 checksum 的完整双目对，保留其余图像原时间戳、序号和所有 IMU。
副本记录剔除帧号、偏移、原文件哈希和明确的相机缺帧位置；只对这些已声明的缺帧允许相机时间 / 参考号跳变。
IMU 连续性检查不放宽，包边界损坏、文件截断及未知相机缺口仍拒绝。网页显示剔除数量，副本再次校验后才生成可复用 bag。

## 标定板尺寸

网页生成 tag36h11、两格黑边、6 列 × 4 行的 AprilGrid，ID 0 位于左下角，向右再向上递增。
标签间距为标签边长的 0.3 倍，四周带黑色小方块作为测量端点。
左右两端小方块外缘之间的跨度是 `6 + 7 × 0.3 = 8.1` 个标签边长。
例如实测跨度 259.2 mm，对应 `tagSize = 0.2592 / 8.1 = 0.032 m`。
后端为每个任务生成 `target.yaml` 并显式传给标定入口，不使用固定 A4 或默认 32 mm 参数。

录制期间网页保持图案的像素大小；进入 / 退出全屏、改变浏览器缩放或页面宽度可能改变实际显示尺寸，
检测到变化后会清空当前实测值，要求重新量尺。录制期间改变显示大小或屏幕缩放后，应重新录制。
浏览器刷新后需要重新测量；已保存录制的尺寸仍保留。浏览器无法从 CSS 单位准确推断物理尺寸。

已有录制使用本页相同 6×4 图案时，手动填写**原录制时**的实测跨度。旧 A4 图案的对应跨度为 259.2 mm
（仅适用于原 32 mm 标签且未被缩放的情况，仍应以实测为准）。其他行列或间距的标定板请使用
`calibration/calibrate.py --target ...`。网页原标定板 PDF 下载入口已移除，命令行标定工具及其原文件保持可用。

空闲预览目标每秒抽取 5 对，录制及保存期间降为每秒 2 对；没有观看者时不编码。
板端独立线程从最新帧槽取得左右图，拼接为灰度 YUV420，交给 VPSS 硬件缩放及 VENC JPEG 编码（质量 80）。
预览合并宽度最多 1280 像素，每目最多 640 像素，保持原宽高比与方向。
浏览器通过一个 `<img>` 直接读取板端 MJPEG；Python 和 SSH 不传输图像，WebSocket 仅推送状态。
板端仍按约 20 Hz 图像 / 200 Hz IMU 完整录制，
录制完整性由录制器独立检查，不依赖网页预览更新频率。
板端使用 Zstandard fast 并显式启用字面量 Huffman 无损编码，保留逐对校验。
压缩和写盘分别在独立线程运行，通过有界队列传递压缩数据；写入阻塞不会同时停止压缩，两个阶段共享 64 对总容量。
编码结果在录制时立即解压与输入比较，压缩包在队列传递和写入前后核对摘要，每对写入并 flush 后用 pread 读回当前帧缓存，核对实际文件字节。停止只排空队列并同步本次录制文件，不再等待板端整文件重读。元数据分别标明 encoding_verified=true、cached_write_verified=true 和 archive_verified=false；取回电脑后完整校验，失败的本地副本改为 INCOMPLETE，不能用于标定。
若录制器异常退出，网页读取设备的最终失败原因，并将该段录制标记为失败，保留不完整数据。

交互参考 [Mighty Calibration](https://mightycamera.com/calib) 的网页显示图案与实测宽度方式，
图案由本项目生成，没有远程图片、字体或脚本依赖。

## 会话与控制接口

原有 `--output ...` 命令行直接录制保持可用。新增会话模式：

```sh
tidesense_record --session --socket /run/tidesense-record.sock --imu /dev/iio:deviceN \
  --http-token "$(openssl rand -hex 16)"
```

持续采集与预览；每段录制独立创建 `Writer`，保留首帧前 IMU 和末帧后 IMU。
写盘收尾由独立线程处理，期间继续消费相机与 IMU。状态依次为：

```text
warming → preview → preparing → recording → finalizing → preview
```

- 控制 socket `/run/tidesense-record.sock`：每行一个 JSON 请求，每行一个 JSON 响应。
  支持 `status`、`start`、`stop`、`close`；`start` 携带绝对 `output` 路径和 `seconds`。
  每段录制有独立 `recording_id`，状态响应带回该标识，旧预览响应不会覆盖新录制状态。
- 控制 socket 权限为 0600。电脑通过 `tidesense_record --relay SOCKET` 建立一个 SSH 控制通道，
  仅传输少量 JSON，不传输图像。SDK 日志保存在设备会话目录的 `session.log`。
- 预览 HTTP 服务由录制器进程内的独立线程提供，启动时绑定空闲端口；`status.preview_port` 返回该端口。
  `--http-token` 必须为 32 位十六进制随机值，网页后端为每个会话生成，预览地址为
  `http://设备IP:端口/TOKEN/stream.mjpg`，另有 `/TOKEN/frame.jpg` 快照接口。
  浏览器需要能直接访问设备地址和该端口。网页后端只向浏览器提供 URL，不代理图像。
- 单个 MJPEG 流包含同一对左右图，响应分片带 Content-Length、X-Sequence、X-Timestamp-Ns。
  同服务还提供受下载授权限制的文件接口，独立文件线程使用 sendfile，不经过 SSH 或 JPEG 编码；最多 2 个文件发送线程。
  服务仅保留最新编码帧，最多 4 个客户端；慢客户端最多持有当前发送帧，发送超时后断开。
  采集、编码和网络发送分别运行，慢客户端不阻塞控制或录制。
- 采集会话将过高的内核串口输出级别临时限制到 4，以免 ISP 刷屏影响 200 Hz IMU；内核环形日志保留，退出时恢复原值。
- 浏览器刷新可以重新订阅当前任务，不会重新启动录制。失败任务不支持断点继续。
- 停止服务会尝试正常停止活动录制、等待保存并关闭采集；关闭浏览器本身不会停止后端任务。

## 验证

```sh
tide_configurator/.venv/bin/pip install -r tide_configurator/requirements-dev.txt
tide_configurator/.venv/bin/python -m pytest tide_configurator/tests -q
cd tide_configurator/frontend && npm run build
```

实机链路、浏览器检查和已知验证边界见 [VALIDATION.md](VALIDATION.md)。
