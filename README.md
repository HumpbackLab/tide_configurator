# TideConfigurator

TideConfigurator 是运行在电脑上的 TideSense 配置工具。通过浏览器连接设备，完成相机与 IMU 数据录制、标定，以及 SchurVINS 的部署和运行管理。当前支持 SG2002 双目 TideSense。

## 功能

- 实时预览双目图像，录制和取回数据集。
- 在网页显示 AprilGrid 标定板，使用实测尺寸标定广角或鱼眼镜头及 IMU。
- 查看标定结果，下载报告和 `schurvins.yaml`，或直接将配置应用到设备。
- 上传 SchurVINS 发布包，启动、停止 VIO，并查看当前配置的 SHA-256。

## 准备

- 电脑：Linux、Python 3.10+、Node.js 22.12+；运行标定还需要 Podman。
- 设备：已安装 `tidesense_record`，可通过 SSH 连接。浏览器需要能访问设备，以查看预览和下载录制。
- 部署 VIO 需要 SchurVINS 发布包；运行 VIO 需要设备上已安装 SchurVINS。

TideConfigurator 自带标定代码，运行时不需要 SG2002 SDK。设备程序路径在 [`boards/tidesense/board.conf`](boards/tidesense/board.conf) 中配置。

## 安装与启动

在项目根目录执行：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
./run.sh
```

打开 <http://127.0.0.1:8765>，选择板型并输入设备的 SSH 地址和凭据。Node.js 只用于构建前端。首次运行标定前，执行：

```sh
./calibration/environment/build.sh
```

## 使用

1. 连接设备，启动预览。在“数据录制”页录制并取回数据，也可以从已有录制记录进入标定。
2. 标定时，在屏幕上显示 AprilGrid，用尺测量左右最外侧黑色小方块的**外缘间距**并填入页面。录制期间保持标定板显示尺寸不变，移动设备获取不同距离和角度的图像。
3. 导入实测 IMU 噪声参数，选择广角或鱼眼镜头并启动标定。完成后可下载结果，或将 `schurvins.yaml` 应用到设备。
4. 在“VIO 部署”页上传 SchurVINS 发布包，并启动或停止 VIO。

标定使用 `scale-misalignment` IMU 模型。录制和标定结果默认保存在项目的 `data/` 目录。

## 板型配置

`boards/<板型>/board.conf` 定义设备地址、录制器、VIO 程序和配置文件等路径；其中的程序和数据路径均指**设备上的路径**。当前仅提供 `boards/tidesense/`，其中也包含 VIO 的部署、启动和停止脚本。

可用环境变量：

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `TIDE_PORT` | 本地网页端口 | `8765` |
| `TIDE_WORKSPACE` | 本地数据目录 | `./data` |
| `KALIBR_IMAGE` | 标定容器镜像 | `localhost/tidesense-calibration:noetic` |

## 开发

后端运行 `./run.sh`；前端开发服务器运行 `npm --prefix frontend run dev`。安装开发依赖后运行测试：

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

## 许可证

[GNU GPLv3](LICENSE)（`GPL-3.0-only`）。
