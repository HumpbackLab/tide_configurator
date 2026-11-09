import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  Camera,
  ChevronDown,
  CircleHelp,
  CheckCircle2,
  Download,
  ExternalLink,
  FileText,
  FolderOpen,
  Grid2X2,
  LoaderCircle,
  Maximize2,
  Moon,
  Play,
  Plug,
  Radio,
  RefreshCw,
  Settings2,
  Square,
  Sun,
  Target,
  Upload,
  Waves,
  X,
} from "lucide-react";
import "./style.css";
import { TargetBoard, type TargetSpec } from "./TargetBoard";
const VioTrajectory = React.lazy(async () => ({
  default: (await import("./VioTrajectory")).VioTrajectory,
}));

type Capture = {
  id: string;
  kind: string;
  remote?: string;
  local?: string;
  host?: string;
  status: string;
  created: string;
  pairs: number;
  elapsed: number;
  error: string;
  board_width_mm?: number;
  prepared?: string;
  preparation_error?: string;
  report?: { dropped_camera_pairs?: number };
};
type Job = {
  id: string;
  config_sha256?: string;
  status: string;
  created: string;
  stage: string;
  lens: string;
  source: string;
  board_width_mm?: number;
  error: string;
  result?: {
    progress?: TransferProgress;
    preparation_reused?: boolean;
    baseline_m: number;
    camera_imu_shifts_s: number[];
    residuals: Record<string, { mean: number; unit: string }>;
    quality: string;
  };
};
type TransferProgress = {
  status: string; stage?: string; bytes: number; total: number; error?: string;
  speed_bps?: number; frames?: number; pairs?: number; cached?: boolean;
};
type CameraCalibrationCapture = {
  gains: number[];
  width: number;
  height: number;
  frames: number;
  created: string;
};
type CameraCalibrationResult = {
  version: number;
  flags: number;
  width: number;
  height: number;
  gains: number[];
  created: string;
  files: Record<string, string>;
  cameras: {
    camera: number;
    buckets: {
      gain: number;
      black_lsb: number;
      dark_reduction: number;
      flat_signal_lsb: number;
      flat_reduction: number;
      flat_saturation: number;
    }[];
  }[];
};
type CameraCalibrationState = {
  status: string;
  stage: string;
  message: string;
  error: string;
  mode: string;
  progress: {
    stage: string;
    message: string;
    index: number;
    total: number;
    gain: number;
  };
  dark?: CameraCalibrationCapture | null;
  flat?: CameraCalibrationCapture | null;
  result?: CameraCalibrationResult | null;
  applied?: { directory: string; files: Record<string, string>; created: string } | null;
};
function transferLabel(t?: TransferProgress) {
  if (!t) return "取回电脑";
  const percent = t.total ? Math.min(100, Math.round(t.bytes / t.total * 100)) : 0;
  if (t.status === "failed") return t.error || "数据准备失败";
  if (t.status === "complete") return "校验、哈希与 bag 已准备好";
  if (t.stage === "recover") return `剔除损坏帧并另存副本 ${percent}%`;
  if (t.stage === "prepare") return `校验、哈希并生成 bag ${percent}% · ${t.frames || 0}/${t.pairs || "—"} 对`;
  if (t.stage === "bag") return `准备 bag ${percent}%`;
  return `HTTP 下载 ${percent}% · ${size(t.bytes)} / ${size(t.total)}${t.speed_bps ? ` · ${size(t.speed_bps)}/s` : ""}`;
}
function TransferStatus({ value }: { value: TransferProgress }) {
  const phase = value.stage === "download" ? 0 : value.status === "complete" ? 2 : 1;
  return <div className="transfer-status" role="status">
    <div className="transfer-stages">{["HTTP 下载", "校验 · 哈希 · bag", "数据就绪"].map((label, index) =>
      <span key={label} className={index <= phase ? "done" : ""}>{label}</span>)}</div>
    <progress max={100} value={value.total ? value.bytes / value.total * 100 : 0} />
    <small className={value.status === "failed" ? "inline-error" : ""}>{transferLabel(value)}</small>
  </div>;
}
type Snapshot = {
  device: {
    connected: boolean;
    board?: string;
    host?: string;
    kernel?: string;
    model?: string;
    free_bytes?: number;
    fingerprint?: string;
  };
  recorder?: {
    state: string;
    pairs: number;
    elapsed: number;
    error: string;
    output: string;
    exposure_max_us?: number;
  };
  frame?: { width: number; height: number; sequence: number; camera_mask?: number };
  preview_url?: string;
  vio?: { state: string; pid?: number; path?: string; config?: string; config_sha256?: string | null; log?: string; last_error?: string };
  vio_transfer?: { status: string; stage: string; bytes: number; total: number; error?: string; backup?: string };
  cpu_percent?: number | null;
  ros?: { connected: boolean; error: string };
  camera_calibration?: CameraCalibrationState;
  error: string;
  captures: Capture[];
  jobs: Job[];
  transfers: Record<
    string,
    TransferProgress
  >;
};
type Board = { id: string; model: string; default_host: string };
const empty: Snapshot = {
  device: { connected: false },
  error: "",
  captures: [],
  jobs: [],
  transfers: {},
  camera_calibration: {
    status: "idle",
    stage: "",
    message: "",
    error: "",
    mode: "",
    progress: { stage: "", message: "", index: 0, total: 0, gain: 0 },
    dark: null,
    flat: null,
    result: null,
    applied: null,
  },
};
const statusNames: Record<string, string> = {
  preparing: "准备录制",
  warming: "采集启动中",
  preview: "预览中",
  recording: "正在录制",
  finalizing: "正在保存",
  complete: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
  replaced: "临时数据已替换",
  running: "运行中",
};
const stageNames: Record<string, string> = {
  preflight: "检查环境",
  export: "校验并导出数据",
  cameras: "双目内外参求解",
  imucam: "相机–IMU 联合求解",
  summary: "导出配置与报告",
  complete: "求解完成",
};
const formatTime = (n = 0) =>
  `${Math.floor(n / 60)
    .toString()
    .padStart(2, "0")}:${Math.floor(n % 60)
    .toString()
    .padStart(2, "0")}`;
const date = (text: string) =>
  new Date(text).toLocaleString("zh-CN", { hour12: false });
const size = (n = 0) =>
  n > 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${(n / 1e6).toFixed(1)} MB`;
async function api(path: string, body?: unknown, method: "POST" | "DELETE" = "POST") {
  const response = await fetch(`/api${path}`, {
    method,
    ...(method === "POST" ? {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    } : {}),
  });
  const result = await response.json();
  if (!response.ok)
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : "请求参数有误，请检查输入",
    );
  return result;
}

function StereoView({ url, frame }: { url?: string; frame?: Snapshot["frame"] }) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => { setFailed(false); setAttempt(0); }, [url]);
  useEffect(() => {
    if (!failed || !url) return;
    const timer = setTimeout(() => { setFailed(false); setAttempt((n) => n + 1); }, 1500);
    return () => clearTimeout(timer);
  }, [failed, url]);
  return (
    <div className="camera-view stereo-view" style={{ aspectRatio: frame ? `${2 * frame.width} / ${frame.height}` : "3.2" }}>
      {[0, 1].map((index) => <div key={index} className={`camera-label stereo-label-${index}`}>
        <span className="tiny-dot" />CAM {index}<span className="camera-name">{index === 0 ? "左目" : "右目"}</span>
      </div>)}
      {url && !failed ? <img key={attempt} src={url} crossOrigin="anonymous" alt="双目实时画面" onError={() => setFailed(true)} /> :
        <div className="camera-empty"><Target size={28} strokeWidth={1} />
          <p>{failed ? "预览连接中断" : "等待相机画面"}</p>
          <small>{failed ? "正在重新连接，可继续控制录制" : "连接设备并启动预览"}</small>
        </div>}
      {url && !failed && frame && [0, 1].map((index) =>
        !((frame.camera_mask ?? 3) & (1 << index)) &&
        <div key={index} className={`camera-unavailable camera-unavailable-${index}`} role="status">
          <Target size={24} strokeWidth={1} />
          <span>{index === 0 ? "左目无图" : "右目无图"}</span>
          <small>另一侧继续预览</small>
        </div>)}
      <div className="camera-footer"><span>{frame ? `${frame.width} × ${frame.height} / 目 · MONO8` : "OV9281 · MONO8"}</span></div>
    </div>
  );
}

function App() {
  const [state, setState] = useState<Snapshot>(empty);
  const [online, setOnline] = useState(false);
  const [tab, setTab] = useState<
    "camera" | "calibration" | "dataset" | "results" | "vio"
  >("calibration");
  const [connectOpen, setConnectOpen] = useState(false);
  const [boards, setBoards] = useState<Board[]>([]);
  const [board, setBoard] = useState("tidesense");
  const [host, setHost] = useState("192.168.123.99");
  const [username, setUsername] = useState("root");
  const [password, setPassword] = useState("");
  const [vioPackage, setVioPackage] = useState<File | null>(null);
  const [vioUploadPercent, setVioUploadPercent] = useState(0);
  const vioPackageInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [lens, setLens] = useState("wide");
  const [resolution, setResolution] = useState("640x400");
  const [exposureDraft, setExposureDraft] = useState("500");
  const [exposureSaved, setExposureSaved] = useState<number | null>(null);
  const [exposureSource, setExposureSource] = useState<"default" | "file">("default");
  const [exposureError, setExposureError] = useState("");
  const [fpnDraft, setFpnDraft] = useState(true);
  const [fpnSaved, setFpnSaved] = useState<boolean | null>(null);
  const [calResolution, setCalResolution] = useState("640x400");
  const [calGains, setCalGains] = useState("1024, 2048, 4096, 7936, 15872");
  const [calFrames, setCalFrames] = useState(40);
  const [calSkip, setCalSkip] = useState(8);
  const [calDarkExposure, setCalDarkExposure] = useState("500");
  const [calFlatExposure, setCalFlatExposure] = useState("500");
  const [seconds, setSeconds] = useState(120);
  const [output, setOutput] = useState("");
  const [noise, setNoise] = useState("");
  const [noiseName, setNoiseName] = useState("");
  const [captureId, setCaptureId] = useState("");
  const [targetSpec, setTargetSpec] = useState<TargetSpec>();
  const [boardWidth, setBoardWidth] = useState("");
  const [solveWidth, setSolveWidth] = useState("");
  const [boardNotice, setBoardNotice] = useState("");
  const [fullscreen, setFullscreen] = useState(false);
  const boardScale = useRef(0);
  const calibrationWorkspace = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [log, setLog] = useState("");
  const [logId, setLogId] = useState("");
  useEffect(() => {
    let alive = true,
      timer: ReturnType<typeof setTimeout>,
      socket: WebSocket;
    function connect() {
      socket = new WebSocket(
        `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/events`,
      );
      socket.onmessage = (event) => {
        if (typeof event.data === "string") {
          setState(JSON.parse(event.data) as Snapshot);
          setOnline(true);
        }
      };
      socket.onclose = () => {
        setOnline(false);
        if (alive) timer = setTimeout(connect, 1500);
      };
      socket.onerror = () => socket.close();
    }
    connect();
    return () => {
      alive = false;
      clearTimeout(timer);
      socket.close();
    };
  }, []);
  useEffect(() => {
    if (!logId) return;
    let alive = true;
    const refresh = () =>
      fetch(`/api/calibrations/${logId}/log`)
        .then((r) => r.json())
        .then((data) => {
          if (alive) setLog(data.text || "");
        })
        .catch(() => {});
    refresh();
    const timer = setInterval(refresh, 1500);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [logId]);
  useEffect(() => {
    if (state.frame) {
      setResolution(`${state.frame.width}x${state.frame.height}`);
    }
  }, [state.frame?.width, state.frame?.height]);
  useEffect(() => {
    if (!state.device.connected) {
      setExposureSaved(null);
      setFpnSaved(null);
      setExposureError("");
      return;
    }
    let active = true;
    fetch("/api/camera/settings")
      .then(async (response) => {
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "无法读取设备相机设置");
        return result as {
          exposure_max_us: number;
          fpn: boolean;
          source: "default" | "file";
        };
      })
      .then((result) => {
        if (active) {
          setExposureSaved(result.exposure_max_us);
          setFpnSaved(result.fpn);
          setFpnDraft(result.fpn);
          setExposureSource(result.source);
          setExposureDraft(String(result.exposure_max_us));
          setExposureError("");
        }
      })
      .catch((reason) => { if (active) setExposureError(String(reason)); });
    return () => { active = false; };
  }, [state.device.connected, state.device.host, state.device.board]);
  useEffect(() => {
    if (!state.device.connected || (tab !== "vio" && tab !== "results")) return;
    const refresh = () => fetch("/api/vio/status").catch(() => {});
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => clearInterval(timer);
  }, [state.device.connected, tab]);
  const recorder = state.recorder;
  const recording = recorder?.state === "recording";
  const recordingBusy =
    !!recorder &&
    ["preparing", "recording", "finalizing"].includes(recorder.state);
  const connected = state.device.connected;
  const settingsUnchanged =
    exposureSaved !== null &&
    Number(exposureDraft) === exposureSaved &&
    fpnSaved !== null &&
    fpnDraft === fpnSaved;
  const cameraCalibration = state.camera_calibration ?? empty.camera_calibration!;
  const calibrationGains = calGains
    .split(/[\s,;]+/)
    .filter(Boolean)
    .map(Number);
  const calibrationGainsValid =
    calibrationGains.length >= 2 &&
    calibrationGains.length <= 8 &&
    new Set(calibrationGains).size === calibrationGains.length &&
    calibrationGains.every(
      (gain) => Number.isInteger(gain) && gain >= 1024 && gain <= 65535,
    );
  const cameraCalibrationRunning = cameraCalibration.status === "running";
  const calibrationBuckets =
    cameraCalibration.result?.cameras.flatMap((camera) => camera.buckets) ?? [];
  const calibrationFlatReduction = calibrationBuckets.length
    ? calibrationBuckets.reduce((sum, bucket) => sum + bucket.flat_reduction, 0) /
      calibrationBuckets.length
    : 0;
  const calibrationDarkReduction = calibrationBuckets.length
    ? calibrationBuckets.reduce((sum, bucket) => sum + bucket.dark_reduction, 0) /
      calibrationBuckets.length
    : 0;
  const calibrationStages: Record<string, string> = {
    capture_dark: "采集暗场",
    capture_flat: "采集平场",
    tool_upload: "上传标定工具",
    capture: "设备采集",
    download: "回传数据",
    compute: "计算 LUT",
    complete: "完成",
  };
  const preview = !!recorder;
  const partialPreview = preview && !!state.frame && (state.frame.camera_mask ?? 3) !== 3;
  const candidates = state.captures.filter((c) => c.status === "complete");
  const selected = candidates.find((c) => c.id === captureId) || candidates[0];
  const jobRunning = state.jobs.some((j) => j.status === "running");
  const transferRunning = Object.values(state.transfers).some((t) => t.status === "running");
  const transfer = selected ? state.transfers[selected.id] : undefined;
  const measured = Number(boardWidth) > 0 && Number(boardWidth) <= 10000;
  const solveMeasured = Number(solveWidth) > 0 && Number(solveWidth) <= 10000;
  useEffect(() => {
    fetch("/api/boards")
      .then((response) => {
        if (!response.ok) throw new Error("无法读取板型配置");
        return response.json() as Promise<Board[]>;
      })
      .then((available) => {
        setBoards(available);
        if (available.length && !available.some((item) => item.id === board)) {
          setBoard(available[0].id);
          setHost(available[0].default_host);
        }
      })
      .catch((reason) => setError(String(reason)));
  }, []);
  useEffect(() => {
    fetch("/api/target")
      .then((response) => {
        if (!response.ok) throw new Error("无法生成标定板，请检查本地服务");
        return response.json();
      })
      .then(setTargetSpec)
      .catch((error) => setError(String(error)));
  }, []);
  useEffect(() => {
    setSolveWidth(selected?.board_width_mm?.toString() || "");
  }, [selected?.id]);
  useEffect(() => {
    const changed = () => setFullscreen(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", changed);
    return () => document.removeEventListener("fullscreenchange", changed);
  }, []);
  function boardResized(pixels: number) {
    if (boardScale.current && Math.abs(boardScale.current - pixels) > 1) {
      setBoardWidth("");
      setBoardNotice(
        "显示尺寸已变化，请重新测量。录制期间改变缩放或显示大小后，需要重新录制。",
      );
    }
    boardScale.current = pixels;
  }
  async function action(name: string, callback: () => Promise<unknown>) {
    setBusy(name);
    setError("");
    try {
      await callback();
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy("");
    }
  }
  function uploadVio(file: File): Promise<void> {
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open("POST", "/api/vio/deploy");
      request.setRequestHeader("Content-Type", "application/octet-stream");
      request.upload.onprogress = (event) => {
        if (event.lengthComputable) setVioUploadPercent(Math.round(event.loaded / event.total * 100));
      };
      request.onload = () => {
        let result: { detail?: string } = {};
        try { result = JSON.parse(request.responseText); } catch { /* invalid response */ }
        if (request.status >= 200 && request.status < 300) resolve();
        else reject(new Error(result.detail || "VIO 部署失败"));
      };
      request.onerror = () => reject(new Error("上传中断，请检查本地服务"));
      request.send(file);
    });
  }
  async function startCalibration() {
    if (!selected) throw new Error("请先完成或导入一份录制");
    if (!noise.trim()) throw new Error("请先导入实测 IMU 噪声参数");
    if (!solveMeasured) throw new Error("请输入这段录制使用的标定板实测跨度");
    await api("/calibrations", {
      capture_id: selected.id,
      lens,
      noise,
      board_width_mm: Number(solveWidth),
    });
    if (document.fullscreenElement) await document.exitFullscreen();
    setTab("results");
  }
  const previewPanel = (
    <section className="panel preview-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">LIVE VIEW</p>
          <h2>双目预览</h2>
        </div>
        <div className="heading-actions">
          <button
            className="icon-button"
            aria-label="放大预览"
            onClick={() => setExpanded(true)}
          >
            <Maximize2 size={17} />
          </button>
        </div>
      </div>
      <div className="camera-pair">
        <StereoView url={expanded ? undefined : state.preview_url} frame={state.frame} />
      </div>
      <div className="preview-status">
        <span className={`live-indicator ${preview ? "active" : ""}`} />
        {partialPreview ? "单目预览 · 双目恢复后可录制" : preview ? "实时预览" : "预览未启动"}
        <span className="preview-divider" />
        <span>原始图像方向</span>
        <span className="push-right">
          {recording ? (
            <>
              <span className="record-dot" /> REC {formatTime(recorder.elapsed)}
            </>
          ) : (
            "预览采样目标 5 Hz"
          )}
        </span>
      </div>
    </section>
  );
  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">
            <Waves size={31} strokeWidth={1.6} />
          </div>
          <div>
            <p className="eyebrow">TIDESENSE</p>
            <h1>TideConfigurator</h1>
            <p className="subtitle">视觉惯性设备配置与标定</p>
          </div>
        </div>
        <div className="topbar-right">
          <span className={`service-indicator ${online ? "online" : ""}`}>
            <i />
            {online ? "本地服务就绪" : "连接本地服务…"}
          </span>
          <button className="text-link" onClick={() => setTab("calibration")}>
            <Grid2X2 size={15} />
            标定板
          </button>
        </div>
      </header>
      <section className="device-card">
        <div className="device-main">
          <div className="device-icon">
            <Radio size={23} />
          </div>
          <div>
            <p className="eyebrow">
              {connected ? "CONNECTED DEVICE" : "DEVICE CONNECTION"}
            </p>
            <h2>{connected ? state.device.model : `连接你的 ${boards.find((item) => item.id === board)?.model || "TideSense"}`}</h2>
            <p className="mono muted">
              {connected
                ? `${state.device.host} · ${state.device.kernel}`
                : "通过局域网连接设备，开始预览与采集"}
            </p>
          </div>
        </div>
        <div className="device-meta">
          <div>
            <span>图像 / 同步</span>
            <strong>
              {preview && state.frame
                ? `${state.frame.width} × ${state.frame.height}`
                : "—"}
              <small>{preview ? "硬件同步 · 约 20 Hz" : "等待设备采集"}</small>
            </strong>
          </div>
          <div>
            <span>连接时可用空间</span>
            <strong>
              {connected ? size(state.device.free_bytes) : "—"}
              <small>
                {preview ? "SC7U22 · 200 Hz" : "图像与 IMU 同步采集"}
              </small>
            </strong>
          </div>
        </div>
        <button
          className={
            connected
              ? "button secondary connect-button"
              : "button primary connect-button"
          }
          disabled={
            !!busy ||
            recordingBusy ||
            Object.values(state.transfers).some((t) => t.status === "running")
          }
          onClick={() =>
            connected
              ? action("disconnect", () => api("/device/disconnect"))
              : setConnectOpen(true)
          }
        >
          {busy === "disconnect" ? (
            <LoaderCircle className="spin" size={16} />
          ) : (
            <Plug size={16} />
          )}
          {connected ? "断开连接" : "连接设备"}
        </button>
      </section>
      <nav className="tabs" aria-label="主要功能">
        {(
          [
            ["camera", "相机校正", Camera],
            ["calibration", "相机标定", Target],
            ["dataset", "数据录制", FolderOpen],
            ["vio", "VIO 部署", Activity],
            ["results", "标定记录", FileText],
          ] as const
        ).map(([key, label, Icon]) => (
          <button
            key={key}
            className={tab === key ? "active" : ""}
            onClick={() => setTab(key)}
            aria-current={tab === key ? "page" : undefined}
          >
            <Icon size={17} />
            {label}
            {key === "results" && state.jobs.length > 0 && (
              <span className="tab-count">{state.jobs.length}</span>
            )}
          </button>
        ))}
      </nav>
      {(error || state.error) && (
        <div className="error-banner" role="alert">
          <CircleHelp size={18} />
          <span>{error || state.error}</span>
          <button
            className="icon-button"
            aria-label="关闭提示"
            onClick={() => setError("")}
          >
            <X size={16} />
          </button>
        </div>
      )}
      {tab === "camera" ? (
        <>
          <div className="page-heading">
            <div>
              <p className="eyebrow">CAMERA FPN CALIBRATION</p>
              <h2>配置曝光与校正双目固定竖纹。</h2>
              <p>
                分别采集遮光暗场和均匀光平场，在电脑上计算黑平衡、暗场列纹和平场列增益，
                再部署为设备启动时加载的 LUT。
              </p>
            </div>
            <span
              className={`badge ${
                cameraCalibration.error || cameraCalibration.status === "failed"
                  ? "bad"
                  : cameraCalibration.result
                    ? "good"
                    : "neutral"
              }`}
            >
              {cameraCalibrationRunning ? (
                <LoaderCircle className="spin" size={13} />
              ) : cameraCalibration.result ? (
                <CheckCircle2 size={13} />
              ) : (
                <Camera size={13} />
              )}
              {cameraCalibrationRunning
                ? calibrationStages[cameraCalibration.stage] ||
                  calibrationStages[cameraCalibration.progress.stage] ||
                  "校正中"
                : cameraCalibration.error || cameraCalibration.status === "failed"
                  ? "校正失败"
                  : cameraCalibration.result
                    ? "LUT 已计算"
                    : "等待采样"}
            </span>
          </div>
          <div className="camera-calibration-grid">
            <section className="panel camera-calibration-panel">
              <div className="panel-heading">
                <div>
                  <p className="eyebrow">DEVICE CAMERA SETTINGS</p>
                  <h3>设备相机设置</h3>
                </div>
                <button
                  className="button light"
                  type="button"
                  disabled={
                    !connected ||
                    !!busy ||
                    preview ||
                    state.vio?.state !== "stopped" ||
                    cameraCalibrationRunning ||
                    !Number.isInteger(Number(exposureDraft)) ||
                    Number(exposureDraft) < 1 ||
                    Number(exposureDraft) > 50000 ||
                    settingsUnchanged
                  }
                  onClick={() =>
                    action("camera-settings", async () => {
                      const result = await api("/camera/settings", {
                        exposure_max_us: Number(exposureDraft),
                        fpn: fpnDraft,
                      });
                      setExposureSaved(result.exposure_max_us);
                      setFpnSaved(result.fpn);
                      setExposureSource("file");
                      setExposureError("");
                    })
                  }
                >
                  {busy === "camera-settings" ? (
                    <LoaderCircle className="spin" size={15} />
                  ) : null}
                  保存设置
                </button>
              </div>
              <div className="camera-calibration-body">
                <label className="field">
                  <span>
                    自动曝光上限 <small>µs</small>
                  </span>
                  <input
                    type="number"
                    min="1"
                    max="50000"
                    step="1"
                    value={exposureDraft}
                    disabled={
                      !connected ||
                      !!busy ||
                      preview ||
                      state.vio?.state !== "stopped" ||
                      cameraCalibrationRunning
                    }
                    onChange={(event) => setExposureDraft(event.target.value)}
                  />
                  <small>
                    设备上的统一设置，预览、录制和 VIO 下次启动时生效。
                  </small>
                  {exposureError && (
                    <small className="inline-error">{exposureError}</small>
                  )}
                </label>
                <div className="field checkbox-field">
                  <span>
                    FPN 校正 <small>黑平衡 + 暗场列纹 + 平场增益</small>
                  </span>
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      checked={fpnDraft}
                      disabled={
                        !connected ||
                        !!busy ||
                        preview ||
                        state.vio?.state !== "stopped" ||
                        cameraCalibrationRunning
                      }
                      onChange={(event) => setFpnDraft(event.target.checked)}
                    />
                    <span>启动相机时应用 FPN LUT</span>
                  </label>
                  <small>
                    需先部署 LUT，并在下次启动预览、录制或 VIO 时生效。
                  </small>
                </div>
                <p className="field-hint">
                  {exposureSaved === null
                    ? "设备设置未读取"
                    : `${exposureSource === "default" ? "设备默认" : "设备已保存"} ${exposureSaved} µs · FPN 校正 ${fpnSaved ? "开" : "关"}`}
                  {recorder?.exposure_max_us
                    ? ` · 当前采集 ${recorder.exposure_max_us} µs`
                    : ""}
                </p>
              </div>
            </section>

            <section className="panel camera-calibration-panel">
              <div className="panel-heading">
                <div>
                  <p className="eyebrow">CAPTURE PARAMETERS</p>
                  <h3>采样参数</h3>
                </div>
                <Settings2 size={19} className="muted" />
              </div>
              <div className="camera-calibration-body">
                <label className="field">
                  <span>标定尺寸</span>
                  <div className="select-wrap">
                    <select
                      value={calResolution}
                      disabled={cameraCalibrationRunning}
                      onChange={(event) => setCalResolution(event.target.value)}
                    >
                      <option value="640x400">640 × 400</option>
                      <option value="640x480">640 × 480</option>
                      <option value="1280x800">1280 × 800</option>
                    </select>
                    <ChevronDown size={15} />
                  </div>
                  <small>LUT 只对相同采集尺寸生效。</small>
                </label>
                <label className="field">
                  <span>
                    模拟增益 <small>Q10，逗号分隔</small>
                  </span>
                  <input
                    value={calGains}
                    disabled={cameraCalibrationRunning}
                    onChange={(event) => setCalGains(event.target.value)}
                    placeholder="1024, 2048, 4096, 7936, 15872"
                  />
                  <small className={calibrationGainsValid ? "" : "inline-error"}>
                    需要 2..8 个不重复的 1024..65535 增益点。
                  </small>
                </label>
                <div className="camera-calibration-inline">
                  <label className="field">
                    <span>帧数</span>
                    <input
                      type="number"
                      min="4"
                      max="200"
                      value={calFrames}
                      disabled={cameraCalibrationRunning}
                      onChange={(event) => setCalFrames(Number(event.target.value))}
                    />
                  </label>
                  <label className="field">
                    <span>丢弃帧</span>
                    <input
                      type="number"
                      min="0"
                      max="100"
                      value={calSkip}
                      disabled={cameraCalibrationRunning}
                      onChange={(event) => setCalSkip(Number(event.target.value))}
                    />
                  </label>
                </div>
                <div className="camera-calibration-inline">
                  <label className="field">
                    <span>
                      暗场曝光 <small>µs</small>
                    </span>
                    <input
                      type="number"
                      min="1"
                      max="50000"
                      value={calDarkExposure}
                      disabled={cameraCalibrationRunning}
                      onChange={(event) => setCalDarkExposure(event.target.value)}
                    />
                  </label>
                  <label className="field">
                    <span>
                      平场曝光 <small>µs</small>
                    </span>
                    <input
                      type="number"
                      min="1"
                      max="50000"
                      value={calFlatExposure}
                      disabled={cameraCalibrationRunning}
                      onChange={(event) => setCalFlatExposure(event.target.value)}
                    />
                  </label>
                </div>
                <p className="field-hint">
                  已启动预览时，采集开始会自动关闭预览。暗场和平场可以使用不同曝光，
                  但两者必须使用相同的增益集合。
                </p>
              </div>
            </section>

            <section className="panel camera-calibration-panel">
              <div className="panel-heading">
                <div>
                  <p className="eyebrow">CAPTURE DARK / FLAT</p>
                  <h3>采集暗场与平场</h3>
                </div>
                <Camera size={19} className="muted" />
              </div>
              <div className="camera-calibration-body">
                <div className="calibration-capture-row">
                  <div>
                    <Moon size={18} />
                    <span>
                      <strong>暗场</strong>
                      <small>
                        {cameraCalibration.dark
                          ? `${cameraCalibration.dark.gains.join(", ")} · ${cameraCalibration.dark.frames} 帧`
                          : "盖住镜头，采集黑电平与暗场列纹"}
                      </small>
                    </span>
                  </div>
                  <button
                    className="button secondary"
                    disabled={
                      !online ||
                      !connected ||
                      !!busy ||
                      recordingBusy ||
                      transferRunning ||
                      jobRunning ||
                      cameraCalibrationRunning ||
                      !calibrationGainsValid
                    }
                    onClick={() => {
                      const [width, height] = calResolution.split("x").map(Number);
                      action("fpn-dark", () =>
                        api("/camera-calibration/capture", {
                          mode: "dark",
                          width,
                          height,
                          gains: calibrationGains,
                          frames: calFrames,
                          skip: calSkip,
                          exposure_us: Number(calDarkExposure),
                        }),
                      );
                    }}
                  >
                    {busy === "fpn-dark" ? (
                      <LoaderCircle className="spin" size={15} />
                    ) : (
                      <Moon size={15} />
                    )}
                    采集暗场
                  </button>
                </div>
                <div className="calibration-capture-row">
                  <div>
                    <Sun size={18} />
                    <span>
                      <strong>平场</strong>
                      <small>
                        {cameraCalibration.flat
                          ? `${cameraCalibration.flat.gains.join(", ")} · ${cameraCalibration.flat.frames} 帧`
                          : "均匀离焦光源，避免高光过曝"}
                      </small>
                    </span>
                  </div>
                  <button
                    className="button secondary"
                    disabled={
                      !online ||
                      !connected ||
                      !!busy ||
                      recordingBusy ||
                      transferRunning ||
                      jobRunning ||
                      cameraCalibrationRunning ||
                      !calibrationGainsValid
                    }
                    onClick={() => {
                      const [width, height] = calResolution.split("x").map(Number);
                      action("fpn-flat", () =>
                        api("/camera-calibration/capture", {
                          mode: "flat",
                          width,
                          height,
                          gains: calibrationGains,
                          frames: calFrames,
                          skip: calSkip,
                          exposure_us: Number(calFlatExposure),
                        }),
                      );
                    }}
                  >
                    {busy === "fpn-flat" ? (
                      <LoaderCircle className="spin" size={15} />
                    ) : (
                      <Sun size={15} />
                    )}
                    采集平场
                  </button>
                </div>
                {cameraCalibrationRunning && (
                  <div className="calibration-progress">
                    <div>
                      <strong>
                        {calibrationStages[cameraCalibration.progress.stage] ||
                          cameraCalibration.progress.message ||
                          "校正中"}
                      </strong>
                      <span>
                        {cameraCalibration.progress.total
                          ? `${cameraCalibration.progress.index + 1} / ${cameraCalibration.progress.total}`
                          : ""}
                      </span>
                    </div>
                    <progress
                      max={Math.max(1, cameraCalibration.progress.total)}
                      value={cameraCalibration.progress.index}
                    />
                    <small>
                      {cameraCalibration.progress.message}
                      {cameraCalibration.progress.gain
                        ? ` · gain ${cameraCalibration.progress.gain}`
                        : ""}
                    </small>
                  </div>
                )}
                {cameraCalibration.error && (
                  <p className="inline-error">{cameraCalibration.error}</p>
                )}
                {!cameraCalibrationRunning && cameraCalibration.message && (
                  <p className="field-hint success">{cameraCalibration.message}</p>
                )}
              </div>
            </section>

            <section className="panel camera-calibration-panel">
              <div className="panel-heading">
                <div>
                  <p className="eyebrow">COMPUTE AND DEPLOY</p>
                  <h3>计算与部署</h3>
                </div>
                <Activity size={19} className="muted" />
              </div>
              <div className="camera-calibration-body">
                <button
                  className="button primary full"
                  disabled={
                    !cameraCalibration.dark ||
                    !cameraCalibration.flat ||
                    !!busy ||
                    cameraCalibrationRunning
                  }
                  onClick={() =>
                    action("fpn-compute", () =>
                      api("/camera-calibration/compute"),
                    )
                  }
                >
                  {busy === "fpn-compute" ? (
                    <LoaderCircle className="spin" size={16} />
                  ) : (
                    <Activity size={16} />
                  )}
                  {busy === "fpn-compute" ? "计算中…" : "计算校正 LUT"}
                </button>
                {cameraCalibration.result ? (
                  <>
                    <div className="result-metrics calibration-result-metrics">
                      <div>
                        <span>增益点</span>
                        <strong>{cameraCalibration.result.gains.length}</strong>
                      </div>
                      <div>
                        <span>暗场列纹降低</span>
                        <strong>
                          {calibrationDarkReduction.toFixed(1)}
                          <small>%</small>
                        </strong>
                      </div>
                      <div>
                        <span>平场列纹降低</span>
                        <strong>
                          {calibrationFlatReduction.toFixed(1)}
                          <small>%</small>
                        </strong>
                      </div>
                    </div>
                    <div className="calibration-camera-list">
                      {cameraCalibration.result.cameras.map((camera) => {
                        const flat = camera.buckets.length
                          ? camera.buckets.reduce(
                              (sum, bucket) => sum + bucket.flat_reduction,
                              0,
                            ) / camera.buckets.length
                          : 0;
                        const dark = camera.buckets.length
                          ? camera.buckets.reduce(
                              (sum, bucket) => sum + bucket.dark_reduction,
                              0,
                            ) / camera.buckets.length
                          : 0;
                        return (
                          <div key={camera.camera}>
                            <span>
                              CAM {camera.camera} ·{" "}
                              {camera.camera === 0 ? "左目" : "右目"}
                            </span>
                            <small>
                              暗场 {dark.toFixed(1)}% · 平场 {flat.toFixed(1)}%
                            </small>
                          </div>
                        );
                      })}
                    </div>
                    <button
                      className="button secondary full"
                      disabled={
                        !connected ||
                        !!busy ||
                        cameraCalibrationRunning ||
                        state.vio?.state !== "stopped"
                      }
                      onClick={() =>
                        action("fpn-apply", () =>
                          api("/camera-calibration/apply"),
                        )
                      }
                    >
                      {busy === "fpn-apply" ? (
                        <LoaderCircle className="spin" size={16} />
                      ) : (
                        <Upload size={16} />
                      )}
                      {busy === "fpn-apply" ? "部署中…" : "部署 LUT 到设备"}
                    </button>
                    {cameraCalibration.applied && (
                      <p className="field-hint success">
                        LUT 已部署到 {cameraCalibration.applied.directory}。
                        重启预览、录制或 VIO 后生效。
                      </p>
                    )}
                    <p className="field-hint">
                      LUT SHA-256：
                      <code className="hash-value">
                        {cameraCalibration.result.files["cam0.fpn"]}
                      </code>
                    </p>
                  </>
                ) : (
                  <p className="field-hint">
                    暗场和平场都采集完成后才能计算。计算在电脑上完成，不上传原始图像。
                  </p>
                )}
              </div>
            </section>
          </div>
        </>
      ) : tab === "calibration" || tab === "dataset" ? (
        <>
          <div className="page-heading">
            <div>
              <p className="eyebrow">
                {tab === "calibration"
                  ? "CALIBRATION WORKSPACE"
                  : "DATASET CAPTURE"}
              </p>
              <h2>
                {tab === "calibration"
                  ? "采集标定数据，校准双目与 IMU。"
                  : "录制双目图像与 IMU 数据。"}
              </h2>
              <p>
                {tab === "calibration"
                  ? "测量下方标定板的实际跨度，保持显示大小固定，移动设备录制。"
                  : "保存原始双目图像与 IMU，供回放、算法验证和离线处理使用。"}
              </p>
            </div>
            <span className="badge neutral">
              <Activity size={14} />
              {recorder ? statusNames[recorder.state] : "等待采集"}
            </span>
          </div>
          <div
            className={`workspace-grid ${tab === "calibration" ? "calibration-workspace" : ""}`}
            ref={calibrationWorkspace}
          >
            <div className="workspace-main">
              {tab === "calibration" ? (
                <section className="panel target-panel">
                  <div className="panel-heading compact">
                    <div>
                      <p className="eyebrow">CALIBRATION TARGET</p>
                      <h2>屏幕标定板</h2>
                    </div>
                    <button
                      className="button light"
                      disabled={recordingBusy}
                      onClick={() =>
                        action("fullscreen", async () => {
                          if (document.fullscreenElement)
                            await document.exitFullscreen();
                          else
                            await calibrationWorkspace.current?.requestFullscreen();
                        })
                      }
                    >
                      <Maximize2 size={15} />{" "}
                      {fullscreen ? "退出全屏" : "全屏显示"}
                    </button>
                  </div>
                  {targetSpec ? (
                    <TargetBoard
                      spec={targetSpec}
                      recording={recordingBusy}
                      onScale={boardResized}
                    />
                  ) : (
                    <p className="target-loading">正在生成标定板…</p>
                  )}
                </section>
              ) : (
                previewPanel
              )}
            </div>
            <aside className="workspace-side">
              {tab === "calibration" && (
                <section className="panel measurement-panel">
                  <div className="panel-heading compact">
                    <div>
                      <p className="eyebrow">TARGET SCALE</p>
                      <h3>测量标定板</h3>
                    </div>
                    <Grid2X2 size={19} className="muted" />
                  </div>
                  <p className="measurement-help">
                    用尺量左端黑色小方块的左外缘，到右端黑色小方块的右外缘。包含两端小方块，不包含白色留边。
                  </p>
                  <label className="field">
                    <span>
                      实测跨度 <small>mm</small>
                    </span>
                    <input
                      type="number"
                      min="0.01"
                      max="10000"
                      step="any"
                      value={boardWidth}
                      disabled={recordingBusy}
                      placeholder="输入尺上量出的距离"
                      onChange={(e) => {
                        setBoardWidth(e.target.value);
                        setBoardNotice("");
                      }}
                    />
                  </label>
                  <p className="measurement-caption">
                    {targetSpec
                      ? `${targetSpec.cols} × ${targetSpec.rows} 标签 · `
                      : ""}
                    不限制显示或纸张尺寸。测量后保持浏览器缩放和标定板大小不变。
                  </p>
                  {boardNotice && (
                    <p className="inline-error" role="status">
                      {boardNotice}
                    </p>
                  )}
                </section>
              )}
              <section className="panel settings-panel">
                <div className="panel-heading">
                  <div>
                    <p className="eyebrow">CAPTURE SETUP</p>
                    <h2>{tab === "calibration" ? "标定设置" : "录制设置"}</h2>
                  </div>
                  <Settings2 size={19} className="muted" />
                </div>
                {tab === "calibration" && (
                  <>
                    <label className="field-label">镜头模式</label>
                    <div className="lens-options">
                      {[
                        ["wide", "广角", "Radtan"],
                        ["fisheye", "鱼眼", "Equidistant"],
                      ].map(([value, label, sub]) => (
                        <button
                          key={value}
                          className={`lens-option ${lens === value ? "chosen" : ""}`}
                          onClick={() => setLens(value)}
                          aria-pressed={lens === value}
                        >
                          <span className={`lens-symbol ${value}`} />
                          <strong>{label}</strong>
                          <small>{sub}</small>
                          <i className="radio-dot" />
                        </button>
                      ))}
                    </div>
                  </>
                )}
                <label className="field">
                  <span>采集尺寸</span>
                  <div className="select-wrap">
                    <select
                      value={resolution}
                      disabled={preview || !!busy}
                      onChange={(e) => setResolution(e.target.value)}
                    >
                      <option value="640x400">640 × 400</option>
                      <option value="640x480">640 × 480</option>
                    </select>
                    <ChevronDown size={15} />
                  </div>
                </label>
                <div className="readonly-pair">
                  <div>
                    <span>相机同步</span>
                    <strong>硬件 · 约 20 Hz</strong>
                  </div>
                  <div>
                    <span>IMU 采样</span>
                    <strong>200 Hz</strong>
                  </div>
                </div>
                <label className="field">
                  <span>
                    录制时长 <small>秒，0 表示手动停止</small>
                  </span>
                  <input
                    type="number"
                    min="0"
                    max="86400"
                    value={seconds}
                    disabled={recordingBusy}
                    onChange={(e) => setSeconds(Number(e.target.value))}
                  />
                </label>
                {tab === "dataset" && (
                  <label className="field">
                    <span>
                      设备录制目录 <small>可选</small>
                    </span>
                    <input
                      value={output}
                      disabled={recordingBusy}
                      onChange={(e) => setOutput(e.target.value)}
                      placeholder="留空自动生成新目录"
                    />
                    <small>已有目录不会被覆盖或删除。</small>
                  </label>
                )}
                <div className="capture-actions">
                  {(error || state.error) && (
                    <p className="capture-error" role="alert">
                      {error || state.error}
                    </p>
                  )}
                  {!preview ? (
                    <button
                      className="button secondary full"
                      disabled={!online || !connected || !!busy}
                      onClick={() =>
                        action("preview", () => {
                          const [width, height] = resolution
                            .split("x")
                            .map(Number);
                          return api("/preview", { width, height });
                        })
                      }
                    >
                      {busy === "preview" ? (
                        <LoaderCircle className="spin" size={17} />
                      ) : (
                        <Play size={17} />
                      )}
                      {busy === "preview" ? "启动采集…" : "启动预览"}
                    </button>
                  ) : recordingBusy ? (
                    <button
                      className="button danger full"
                      disabled={recorder.state !== "recording" || !!busy}
                      onClick={() =>
                        action("stop", () => api("/recordings/stop"))
                      }
                    >
                      {recorder.state === "finalizing" ||
                      recorder.state === "preparing" ? (
                        <LoaderCircle className="spin" size={16} />
                      ) : (
                        <Square size={15} />
                      )}
                      {recorder.state === "recording"
                        ? `停止录制 · ${formatTime(recorder.elapsed)}`
                        : statusNames[recorder.state]}
                    </button>
                  ) : (
                    <button
                      className="button primary full"
                      disabled={
                        !!busy || partialPreview ||
                        (tab === "calibration" && (!measured || !targetSpec)) ||
                        Object.values(state.transfers).some(
                          (t) => t.status === "running",
                        )
                      }
                      onClick={() =>
                        action("record", () =>
                          api("/recordings", {
                            kind: tab,
                            output,
                            seconds,
                            ...(tab === "calibration"
                              ? { board_width_mm: Number(boardWidth) }
                              : {}),
                          }),
                        )
                      }
                    >
                      {busy === "record" ? (
                        <LoaderCircle className="spin" size={17} />
                      ) : (
                        <span className="button-record-dot" />
                      )}
                      开始录制
                    </button>
                  )}
                  <p className="field-hint">
                    {recordingBusy
                      ? `${recorder.pairs} 对图像已提交 · 预览目标 2 Hz · 停止后等待写盘完成`
                      : tab === "calibration"
                        ? "标定使用专用临时目录，下次标定录制时重建。"
                        : "保存原始图像和 IMU，无损压缩，不运行 VIO。"}
                  </p>
                </div>
              </section>
              {tab === "calibration" && previewPanel}
              {tab === "calibration" && state.captures.filter((c) => (c.kind === "imported" || c.kind === "recovered") && state.transfers[c.id]?.status !== "complete" && state.transfers[c.id]).map((c) => <TransferStatus key={c.id} value={state.transfers[c.id]} />)}
              {tab === "calibration" && (
                <section className="panel solve-panel">
                  <div className="panel-heading compact">
                    <div>
                      <p className="eyebrow">CALIBRATION</p>
                      <h3>准备求解</h3>
                    </div>
                    <Activity size={19} className="muted" />
                  </div>
                  <label className="field">
                    <span>录制数据</span>
                    <div className="select-wrap">
                      <select
                        value={selected?.id || ""}
                        onChange={(e) => setCaptureId(e.target.value)}
                        disabled={!candidates.length}
                      >
                        <option value="" disabled>
                          完成录制或导入数据
                        </option>
                        {candidates.map((c) => (
                          <option value={c.id} key={c.id}>
                            {date(c.created)} · {c.pairs} 对
                          </option>
                        ))}
                      </select>
                      <ChevronDown size={15} />
                    </div>
                  </label>
                  <label className="field">
                    <span>
                      录制时标定板跨度 <small>mm</small>
                    </span>
                    <input
                      type="number"
                      min="0.01"
                      max="10000"
                      step="any"
                      value={solveWidth}
                      onChange={(e) => setSolveWidth(e.target.value)}
                      placeholder="这段数据录制时的实测跨度"
                    />
                    <small>
                      使用本页 6 × 4
                      图案。尺寸取自该段录制，不随当前屏幕显示大小更新。
                    </small>
                  </label>
                  <label className="file-upload">
                    <input
                      type="file"
                      accept=".yaml,.yml"
                      onChange={async (e) => {
                        const file = e.target.files?.[0];
                        if (file) {
                          setNoise(await file.text());
                          setNoiseName(file.name);
                        }
                      }}
                    />
                    <Upload size={17} />
                    <span>
                      <strong>{noiseName || "导入 IMU 噪声参数"}</strong>
                      <small>
                        {noiseName
                          ? "参数已载入，可展开检查"
                          : "选择实测噪声 YAML 文件"}
                      </small>
                    </span>
                    <span className="file-extension">YAML</span>
                  </label>
                  {noise && (
                    <details className="noise-details">
                      <summary>查看噪声参数</summary>
                      <textarea
                        aria-label="IMU 噪声参数"
                        value={noise}
                        onChange={(e) => setNoise(e.target.value)}
                        rows={6}
                      />
                    </details>
                  )}
                  <div className="model-row">
                    <span>IMU 模型</span>
                    <code>scale-misalignment</code>
                  </div>
                  {selected && !selected.local ? (
                    <>
                      <button
                        className="button light full"
                        disabled={!!busy || transfer?.status === "running"}
                        onClick={() =>
                          action("retrieve", () =>
                            api(`/captures/${selected.id}/retrieve`),
                          )
                        }
                      >
                        {transfer?.status === "running" ? (
                          <LoaderCircle className="spin" size={16} />
                        ) : (
                          <ArrowDownToLine size={16} />
                        )}
                        {transfer?.status === "running"
                          ? "正在准备数据…"
                          : "将录制取回电脑"}
                      </button>
                      {transfer?.status === "failed" && <>
                        <p className="inline-error">{transfer.error}</p>
                        {transfer.stage === "prepare" && <button className="button light full" disabled={!!busy} onClick={() => action("recover", async () => {
                          const copy = await api(`/captures/${selected.id}/recover`); setCaptureId(copy.id);
                        })}>剔除坏帧后另存并准备标定</button>}
                      </>}
                    </>
                  ) : (
                    <button
                      className="button primary full"
                      disabled={
                        !selected?.local ||
                        !noise.trim() ||
                        !solveMeasured ||
                        !!busy ||
                        jobRunning
                      }
                      onClick={() => action("calibrate", startCalibration)}
                    >
                      {jobRunning ? (
                        <LoaderCircle className="spin" size={17} />
                      ) : (
                        <ArrowRight size={17} />
                      )}
                      {jobRunning ? "标定任务运行中" : "开始标定"}
                    </button>
                  )}
                  {!!selected?.report?.dropped_camera_pairs && <p className="field-hint">已剔除 {selected.report.dropped_camera_pairs} 对损坏图像，保留原时间戳和完整 IMU。</p>}
                  {transfer && <TransferStatus value={transfer} />}
                  {selected?.prepared && !transfer && <p className="field-hint">已准备好校验、哈希与 bag，标定时直接复用。</p>}
                  <p className="field-hint">
                    同一份录制依次求解双目与相机–IMU 参数。
                    <a
                      className="inline-link"
                      href="/api/noise-template.yaml"
                      download
                    >
                      下载噪声参数模板
                    </a>
                  </p>
                </section>
              )}
            </aside>
          </div>
          {tab === "dataset" && (
            <section className="panel history-panel">
              <div className="panel-heading compact">
                <h3>录制记录</h3>
                <span className="muted">{state.captures.length} 份</span>
              </div>
              {state.captures.length ? (
                state.captures.map((c) => (
                  <div className="capture-entry" key={c.id} data-capture-id={c.id}><div className="capture-row">
                    <FolderOpen size={17} />
                    <div>
                      <strong>{c.local || c.remote}</strong>
                      <small>
                        {date(c.created)} · {c.pairs} 对 ·{" "}
                        {formatTime(c.elapsed)}
                      </small>
                    </div>
                    <span
                      className={`badge ${c.status === "complete" ? "good" : "neutral"}`}
                    >
                      {statusNames[c.status] || c.status}
                    </span>
                  </div>{state.transfers[c.id] && <TransferStatus value={state.transfers[c.id]} />}
                    <div className="capture-entry-actions">
                      {c.status === "complete" && !c.local && (
                        <button className="button light" disabled={!!busy || state.transfers[c.id]?.status === "running"}
                          onClick={() => action(`retrieve-${c.id}`, () => api(`/captures/${c.id}/retrieve`))}>
                          {state.transfers[c.id]?.status === "running" ? "正在准备…" : "取回电脑"}
                        </button>
                      )}
                      {c.status === "complete" && <button className="button light" disabled={!!busy} onClick={() => {
                        setCaptureId(c.id);
                        setTab("calibration");
                        setTimeout(() => document.querySelector(".solve-panel")?.scrollIntoView({ behavior: "smooth", block: "start" }), 0);
                      }}>标定</button>}
                      {state.transfers[c.id]?.status === "failed" && state.transfers[c.id]?.stage === "prepare" &&
                        <button className="button light" disabled={!!busy} onClick={() => action(`recover-${c.id}`, async () => {
                          const copy = await api(`/captures/${c.id}/recover`); setCaptureId(copy.id);
                        })}>剔除坏帧后另存副本</button>}
                      <button className="button danger" disabled={!!busy || recordingBusy || jobRunning || transferRunning}
                        onClick={() => {
                          if (!window.confirm("删除这条记录及 TideConfigurator 管理的录制文件？导入的外部原始目录不会删除。此操作无法撤销。")) return;
                          action(`delete-${c.id}`, async () => {
                            await api(`/captures/${c.id}`, undefined, "DELETE");
                            if (captureId === c.id) setCaptureId("");
                          });
                        }}>删除</button>
                    </div>
                    {!!c.report?.dropped_camera_pairs && <p className="field-hint">已剔除 {c.report.dropped_camera_pairs} 对坏帧，保留原时间戳和完整 IMU。</p>}
                  </div>
                ))
              ) : (
                <div className="empty-history">
                  <FolderOpen size={26} />
                  <p>还没有录制数据</p>
                  <span>连接设备后，开始一份新的数据集录制。</span>
                </div>
              )}
            </section>
          )}
        </>
      ) : tab === "vio" ? (
        <>
          <div className="page-heading">
            <div>
              <p className="eyebrow">VIO RUNTIME</p>
              <h2>部署并运行 SchurVINS</h2>
              <p>部署 SchurVINS，查看 ROS 2 实时轨迹与设备 CPU 占用率。</p>
            </div>
          </div>
          <React.Suspense fallback={<section className="panel vio-trajectory-panel vio-trajectory-loading">正在载入轨迹视图…</section>}>
            <VioTrajectory cpuPercent={state.cpu_percent} rosError={state.ros?.error} />
          </React.Suspense>
          <div className="vio-grid">
            <section className="panel vio-panel">
              <div className="panel-heading">
                <div><p className="eyebrow">RUN STATUS</p><h3>运行状态</h3></div>
                <span className={`badge ${state.vio?.state === "running" || state.vio?.state === "external" ? "good" : "neutral"}`}>
                  {state.vio?.state === "running" ? `运行中 · PID ${state.vio.pid}` :
                   state.vio?.state === "external" ? `外部启动 · PID ${state.vio.pid}` :
                   state.vio?.state === "stopped" ? "已停止" : connected ? "状态未知" : "未连接"}
                </span>
              </div>
              <div className="vio-panel-body">
                <div className="vio-paths">
                  <div><span>启动程序</span><code>{state.vio?.path || "连接设备后显示"}</code></div>
                  <div><span>标定配置</span><code>{state.vio?.config || "连接设备后显示"}</code></div>
                  <div><span>设备配置 SHA-256</span><code className="hash-value">{state.vio?.config_sha256 || (connected ? "无法读取" : "连接设备后显示")}</code></div>
                  <div><span>运行日志</span><code>{state.vio?.log || "连接设备后显示"}</code></div>
                </div>
                <p className="field-hint">启动前会关闭相机预览；录制和数据取回完成后才能启动 VIO。</p>
                {state.vio?.state === "stopped" && state.vio.last_error &&
                  <p className="field-hint inline-error">上次退出：{state.vio.last_error}</p>}
              </div>
              <div className="vio-panel-footer">
                <button className="button primary" disabled={!connected || !!busy || recordingBusy || transferRunning || state.vio?.state !== "stopped"}
                  onClick={() => action("vio-start", () => api("/vio/start"))}>
                  {busy === "vio-start" ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
                  启动 VIO
                </button>
                <button className="button light" disabled={!connected || !!busy || !["running", "external"].includes(state.vio?.state || "")}
                  onClick={() => action("vio-stop", () => api("/vio/stop"))}>
                  {busy === "vio-stop" ? <LoaderCircle className="spin" size={16} /> : <Square size={15} />}
                  停止 VIO
                </button>
              </div>
            </section>
            <section className="panel vio-panel">
              <div className="panel-heading">
                <div><p className="eyebrow">INSTALL PACKAGE</p><h3>上传部署</h3></div>
              </div>
              <div className="vio-panel-body">
                <p className="vio-description">选择当前板型的 SchurVINS 发布包。安装前会校验文件，保留设备上的标定配置，并备份旧程序。</p>
                <label className={`file-upload vio-file-upload ${!connected || !!busy ? "disabled" : ""}`}>
                  <input ref={vioPackageInput} type="file" accept=".tar,.tar.gz,.tgz" disabled={!connected || !!busy}
                    onChange={(event) => setVioPackage(event.target.files?.[0] || null)} />
                  <Upload size={19} />
                  <span>
                    <strong>{vioPackage?.name || "选择本地发布包"}</strong>
                    <small>{vioPackage ? size(vioPackage.size) : "支持 .tar 和 .tar.gz"}</small>
                  </span>
                  <span className="file-extension">VIO</span>
                </label>
                {busy === "vio-deploy" && state.vio_transfer?.status !== "running" &&
                  <p className="field-hint">上传到本地服务 {vioUploadPercent}%</p>}
                {state.vio_transfer?.status === "running" && <div className="vio-progress">
                  <span>{state.vio_transfer.stage === "browser_upload" ? "接收本地文件" :
                         state.vio_transfer.stage === "verify" ? "检查发布包" :
                         state.vio_transfer.stage === "device_upload" ? "上传到设备" : "在设备上安装"}</span>
                  {state.vio_transfer.total > 0 &&
                    <progress max={state.vio_transfer.total} value={state.vio_transfer.bytes} />}
                </div>}
                {state.vio_transfer?.status === "complete" &&
                  <p className="field-hint success">部署完成。{state.vio_transfer.backup && `旧版本备份：${state.vio_transfer.backup}`}</p>}
                {state.vio_transfer?.status === "failed" &&
                  <p className="field-hint inline-error">{state.vio_transfer.error}</p>}
              </div>
              <div className="vio-panel-footer">
                <button className="button secondary" disabled={!connected || !vioPackage || !!busy || recordingBusy || transferRunning || state.vio?.state !== "stopped"}
                  onClick={() => action("vio-deploy", async () => {
                    if (!vioPackage) return;
                    setVioUploadPercent(0);
                    await uploadVio(vioPackage);
                    setVioPackage(null);
                    if (vioPackageInput.current) vioPackageInput.current.value = "";
                  })}>
                  {busy === "vio-deploy" ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}
                  {busy === "vio-deploy" ? "部署中…" : "上传并部署"}
                </button>
              </div>
            </section>
          </div>
        </>
      ) : (
        <>
          <div className="page-heading">
            <div>
              <p className="eyebrow">CALIBRATION HISTORY</p>
              <h2>标定任务与结果</h2>
              <p>查看任务状态、求解日志和结果报告，下载或应用 VIO 配置。</p>
            </div>
            <button
              className="button light"
              onClick={() => setTab("calibration")}
            >
              <Target size={16} />
              新建标定
            </button>
          </div>
          {!state.jobs.length ? (
            <section className="panel empty-results">
              <div className="empty-result-icon">
                <FileText size={32} strokeWidth={1.3} />
              </div>
              <h3>还没有标定任务</h3>
              <p>完成录制后，在“数据录制”的记录中点击“标定”。</p>
              <button
                className="button primary"
                onClick={() => setTab("calibration")}
              >
                进入相机标定
                <ArrowRight size={16} />
              </button>
            </section>
          ) : (
            state.jobs.map((job) => (
              <section className="panel job-panel" key={job.id}>
                <div className="panel-heading">
                  <div>
                    <p className="eyebrow">
                      {date(job.created)} ·{" "}
                      {job.lens === "wide" ? "广角" : "鱼眼"}
                      {job.board_width_mm
                        ? ` · 标定板跨度 ${job.board_width_mm} mm`
                        : ""}
                    </p>
                    <h3>
                      {job.status === "running"
                        ? stageNames[job.stage] || "正在标定"
                        : job.status === "complete"
                          ? "标定求解完成"
                          : `标定${statusNames[job.status] || job.status}`}
                    </h3>
                  </div>
                  <span
                    className={`badge ${job.status === "complete" ? "good" : job.status === "failed" ? "bad" : "neutral"}`}
                  >
                    {job.status === "running" && (
                      <LoaderCircle className="spin" size={13} />
                    )}
                    {statusNames[job.status]}
                  </span>
                </div>
                {job.status === "running" && (
                  <div className="job-progress">
                    {["export", "cameras", "imucam", "summary"].map((s, i) => (
                      <div
                        key={s}
                        className={
                          ["export", "cameras", "imucam", "summary"].indexOf(
                            job.stage,
                          ) >= i
                            ? "done"
                            : ""
                        }
                      >
                        <span />
                        {stageNames[s]}
                      </div>
                    ))}
                  </div>
                )}
                {job.status === "running" && job.stage === "export" && job.result?.progress && <p className="field-hint">{job.result.progress.cached ? "复用已校验的 bag 与输入哈希" : transferLabel({ ...job.result.progress, status: "running" })}</p>}
                {job.result?.preparation_reused && <p className="field-hint">本次复用了已校验的 bag 与输入哈希。</p>}
                {job.status === "complete" && job.result && (
                  <>
                    <div className="result-metrics">
                      <div>
                        <span>实测双目基线</span>
                        <strong>
                          {(job.result.baseline_m * 1000).toFixed(3)}
                          <small>mm</small>
                        </strong>
                      </div>
                      <div>
                        <span>左相机时间偏移</span>
                        <strong>
                          {(job.result.camera_imu_shifts_s[0] * 1000).toFixed(
                            3,
                          )}
                          <small>ms</small>
                        </strong>
                      </div>
                      <div>
                        <span>质量状态</span>
                        <strong className="review-label">待复核</strong>
                      </div>
                    </div>
                    <p className="panel-note">
                      <CircleHelp size={14} />
                      求解完成不等于精度合格，请检查 PDF 报告及独立 VIO
                      运行结果。
                    </p>
                  </>
                )}
                {job.status === "complete" && job.config_sha256 && (
                  <div className="calibration-config-hash">
                    <div className="calibration-config-hash-heading">
                      <span>本次标定 VIO 配置 SHA-256</span>
                      {connected && state.vio?.config_sha256 === job.config_sha256 &&
                        <span className="badge good">当前使用</span>}
                    </div>
                    <code className="hash-value">{job.config_sha256}</code>
                  </div>
                )}
                {job.error && <pre className="job-error">{job.error}</pre>}
                <div className="job-actions">
                  <button
                    className="button light"
                    onClick={() => setLogId(job.id)}
                  >
                    <FileText size={15} />
                    查看日志
                  </button>
                  {job.status !== "running" && (
                    <button className="button danger-outline" disabled={!!busy}
                      onClick={() => {
                        if (!window.confirm("删除这条标定记录、结果和报告？原始录制数据不会删除。此操作无法撤销。")) return;
                        action(`delete-job-${job.id}`, async () => {
                          await api(`/calibrations/${job.id}`, undefined, "DELETE");
                          if (logId === job.id) { setLogId(""); setLog(""); }
                        });
                      }}>删除</button>
                  )}
                  {job.status === "running" && (
                    <button
                      className="button danger-outline"
                      onClick={() =>
                        action("cancel", () =>
                          api(`/calibrations/${job.id}/cancel`),
                        )
                      }
                    >
                      取消任务
                    </button>
                  )}
                  {job.status === "complete" && (
                    <>
                      <a
                        className="button light"
                        href={`/api/calibrations/${job.id}/files/dataset-report-cam.pdf`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        双目报告
                        <ExternalLink size={14} />
                      </a>
                      <a
                        className="button light"
                        href={`/api/calibrations/${job.id}/files/dataset-report-imucam.pdf`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        相机–IMU 报告
                        <ExternalLink size={14} />
                      </a>
                      <div className="job-vio-actions push-right">
                        <a
                          className="button primary"
                          href={`/api/calibrations/${job.id}/files/schurvins.yaml`}
                          download
                        >
                          <Download size={15} />
                          下载 VIO 配置
                        </a>
                        <button
                          className="button secondary"
                          disabled={!connected || !!busy || state.vio?.state !== "stopped"}
                          title={state.vio?.state !== "stopped" ? "请先停止 VIO" : undefined}
                          onClick={() => action(`apply-${job.id}`, async () => {
                            await api(`/calibrations/${job.id}/apply`);
                          })}
                        >
                          {busy === `apply-${job.id}` ? <LoaderCircle className="spin" size={15} /> : <Upload size={15} />}
                          {busy === `apply-${job.id}` ? "应用中…" : "应用到设备"}
                        </button>
                      </div>
                    </>
                  )}
                </div>
              </section>
            ))
          )}
        </>
      )}
      <footer>
        <span>
          <Waves size={16} /> TideSense
        </span>
        <span>本地运行 · 原始数据留在你的设备与电脑</span>
        <span className="footer-end">TideConfigurator</span>
      </footer>
      {connectOpen && (
        <div
          className="modal-backdrop"
          onClick={() => !busy && setConnectOpen(false)}
        >
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="connect-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-heading">
              <div>
                <p className="eyebrow">DEVICE CONNECTION</p>
                <h2 id="connect-title">连接设备</h2>
              </div>
              <button
                className="icon-button"
                aria-label="关闭连接窗口"
                disabled={!!busy}
                onClick={() => setConnectOpen(false)}
              >
                <X size={20} />
              </button>
            </div>
            <p className="muted modal-description">
              电脑和设备需在同一网络，使用 SSH 建立连接。
            </p>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                action("connect", async () => {
                  await api("/device/connect", { board, host, username, password });
                  setPassword("");
                  setConnectOpen(false);
                });
              }}
            >
              <label className="field">
                <span>板型</span>
                <select value={board} onChange={(event) => {
                  const selected = boards.find((item) => item.id === event.target.value);
                  setBoard(event.target.value);
                  if (selected) setHost(selected.default_host);
                }}>
                  {boards.map((item) => <option key={item.id} value={item.id}>{item.model}</option>)}
                </select>
              </label>
              <label className="field">
                <span>设备地址</span>
                <input
                  autoFocus
                  value={host}
                  onChange={(e) => setHost(e.target.value)}
                  placeholder="192.168.123.99"
                  required
                  autoComplete="off"
                />
              </label>
              <label className="field">
                <span>用户名</span>
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  required
                  autoComplete="username"
                />
              </label>
              <label className="field">
                <span>密码</span>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                  placeholder="留空使用本机 SSH 密钥"
                />
              </label>
              {error && (
                <p className="inline-error" role="alert">
                  {error}
                </p>
              )}
              <div className="modal-actions">
                <button
                  className="button light"
                  type="button"
                  disabled={!!busy}
                  onClick={() => setConnectOpen(false)}
                >
                  取消
                </button>
                <button className="button primary" disabled={!!busy}>
                  {busy === "connect" ? (
                    <LoaderCircle className="spin" size={17} />
                  ) : (
                    <Plug size={16} />
                  )}
                  {busy === "connect" ? "连接中…" : "连接设备"}
                </button>
              </div>
            </form>
          </section>
        </div>
      )}
      {expanded && (
        <div className="modal-backdrop" onClick={() => setExpanded(false)}>
          <section
            className="expanded-preview"
            role="dialog"
            aria-modal="true"
            aria-label="双目预览"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-heading">
              <h3>双目实时预览</h3>
              <button
                className="icon-button"
                aria-label="关闭放大预览"
                onClick={() => setExpanded(false)}
              >
                <X size={21} />
              </button>
            </div>
            <div className="camera-pair">
              <StereoView url={state.preview_url} frame={state.frame} />
            </div>
          </section>
        </div>
      )}
      {logId && (
        <div className="modal-backdrop" onClick={() => setLogId("")}>
          <section
            className="log-modal"
            role="dialog"
            aria-modal="true"
            aria-label="求解日志"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-heading">
              <h3>求解日志</h3>
              <button
                className="icon-button"
                aria-label="关闭日志"
                onClick={() => setLogId("")}
              >
                <X size={20} />
              </button>
            </div>
            <pre className="log-output">{log || "等待日志…"}</pre>
          </section>
        </div>
      )}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
