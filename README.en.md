# TideConfigurator

[简体中文](README.md) | English

TideConfigurator is a TideSense configuration tool that runs on your computer.
Connect to the device through a browser to record camera and IMU data, run calibration,
and deploy and manage SchurVINS. It currently supports SG2002 stereo TideSense devices.

## Features

- Preview stereo images live, record datasets, and download recordings.
- Keep previewing the available camera if one side stops producing frames, with the missing side marked. Recording requires both cameras.
- Set the device's automatic exposure limit for the next preview, recording, or VIO session.
- Display an AprilGrid calibration target in the browser and use its measured dimensions to calibrate wide-angle or fisheye cameras and the IMU.
- Inspect calibration results, download reports and `schurvins.yaml`, or apply the configuration directly to the device.
- Upload a SchurVINS release package, start and stop VIO, and view the SHA-256 of the current configuration.
- View the live ROS 2 3D trajectory, tracking status, and device CPU usage on the VIO page.

## Requirements

- Computer: Linux, Python 3.10+, and Node.js 22.12+. Live VIO trajectories also require ROS 2 and `rclpy`; calibration requires Podman.
- Device: a recent version of `tidesense_record`, accessible over SSH. The browser must be able to reach the device for preview and recording downloads.
- Deploying VIO requires a SchurVINS release package; running VIO requires SchurVINS to be installed on the device.

TideConfigurator includes its own calibration code and does not require the SG2002 SDK at runtime.
Device program paths are configured in [`boards/tidesense/board.conf`](boards/tidesense/board.conf).

## Install and start

Run these commands from the TideConfigurator project root:

```sh
python3 -m pip install -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
./run.sh
```

`run.sh` uses `python3` from the current `PATH`. If you want a virtual environment, create and activate
one wherever you prefer, then install the dependencies into your chosen Python environment.

SSH host keys are shared with the user running the configurator through `~/.ssh/known_hosts`.
New host keys are saved automatically; connections are rejected if a cached key differs.
The legacy `data/known_hosts` file is no longer read. After confirming that reflashing or replacing
the device changed its key, run `ssh-keygen -R 192.168.123.99` to remove the old entry, then reconnect.

Open <http://127.0.0.1:8765>, select the board type, and enter the device's SSH address and credentials.
Node.js is only used to build the frontend. A frontend build is optional when using the frontend
development server or running only the API. Before running calibration for the first time, run:

```sh
./calibration/environment/build.sh
```

## Usage

1. Connect to the device and start preview. Record and download data on the data recording page, or start calibration from an existing recording.
2. For calibration, display the AprilGrid on screen. Measure the **distance between the outer edges** of the leftmost and rightmost black squares with a ruler and enter it on the page. Keep the displayed target size unchanged during recording, and move the device to capture images at different distances and angles.
3. Import measured IMU noise parameters, select a wide-angle or fisheye lens model, and start calibration. When it finishes, download the results or apply `schurvins.yaml` to the device.
4. On the VIO deployment page, upload a SchurVINS release package using native ROS 2 DDS. Start VIO to view its live trajectory and CPU usage.

SchurVINS uses native ROS 2 DDS by default and publishes `/tidesense/pose` and `/tidesense/tracking`.
The configurator subscribes directly on the computer; the device and computer must use the same ROS domain
and be able to discover each other. The browser receives trajectories over a local WebSocket, without
connecting directly to ROS 2. CPU usage is read from the device's `/proc/stat` and updated once per second.

When starting VIO, the configurator obtains both addresses from the current SSH connection,
configures unicast DDS discovery, and forwards the host's `ROS_DOMAIN_ID` (default 0) to the device.
No fixed IP or extra address entry is needed. The host defaults to Cyclone DDS and preserves explicit
`RMW_IMPLEMENTATION` and `CYCLONEDDS_URI` settings. VIO continues after closing the browser;
after a device reboot, start it again from the configurator. Boot autostart is not configured.

The configurator passes `SCHUR_PREDICTION=0` to SchurVINS so that ROS 2 publishes poses at the visual
correction rate only (approximately 20 Hz). Every received ROS 2 pose is written with its original timestamp
to `data/vio-trajectories/<session-id>.jsonl` on the computer, with no point limit. The VIO page can download
the full record for the current session. The browser draws the trajectory incrementally in chunks, retaining
all received points, and displays position XYZ in meters and Roll/Pitch/Yaw in degrees, converted from the
quaternion using the ZYX Euler convention. The device's `poses.csv` retains the estimator's visual correction
poses; the computer's JSONL file records the ROS 2 messages actually received.

Before using live VIO trajectories, load your ROS 2 environment yourself (for example,
`source /opt/ros/<distro>/setup.bash`) and ensure that the current Python can import `rclpy`.
`run.sh` inherits your ROS domain, DDS implementation, and DDS configuration.
If host ROS 2 is unavailable, device-side VIO can still start; the page displays the trajectory subscription error.
Other features do not require a ROS 2 environment.
When started manually on the device, the native DDS version of SchurVINS publishes ROS 2 messages by
default; set `SCHUR_ROS=0` to disable publishing.

Calibration uses the `scale-misalignment` IMU model. Recordings and calibration results are saved in
the project's `data/` directory by default.
The automatic exposure limit is stored on the device in `/mnt/data/tide-configurator/camera.conf`,
with content such as `exposure_max_us=1200`. Change it in the recording settings or edit the file directly;
the setting takes effect when capture is restarted. The file path is specified by the board configuration.

## Board configuration

`boards/<board-type>/board.conf` defines the device address and paths for the recorder, VIO program,
configuration files, and other resources. Program and data paths in this file refer to **paths on the device**.
Currently, only `boards/tidesense/` is provided; it also includes scripts to deploy, start, and stop VIO.

Available environment variables:

| Variable | Purpose | Default |
| --- | --- | --- |
| `TIDE_PORT` | Local web server port | `8765` |
| `TIDE_WORKSPACE` | Local data directory | `./data` |
| `KALIBR_IMAGE` | Calibration container image | `localhost/tidesense-calibration:noetic` |
| `TIDE_FRONTEND_DIST` | Built frontend directory | `./frontend/dist` |

## Development

Run the backend with `./run.sh` and the frontend development server with `npm --prefix frontend run dev`.
Install the development dependencies and run the tests:

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests -q
```

## License

[GNU GPLv3](LICENSE) (`GPL-3.0-only`).
