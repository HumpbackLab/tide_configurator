import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

type Point = {
  seq: number;
  segment: number;
  position: number[];
  orientation: number[];
  frame: string;
};
type Packet = { reset: boolean; points: Point[]; tracking: boolean; frame: string | null };
type PoseReadout = { position: number[]; rpy: number[] };
type PathChunk = { line: THREE.Line; positions: Float32Array; count: number; segment: number };
const CHUNK_POINTS = 1024;

const toView = (position: number[]) => new THREE.Vector3(position[0], position[2], -position[1]);
const basis = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2);
const basisInverse = basis.clone().invert();

export function VioTrajectory({ cpuPercent, rosError }: { cpuPercent?: number | null; rosError?: string }) {
  const host = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [following, setFollowing] = useState(true);
  const [connected, setConnected] = useState(false);
  const [tracking, setTracking] = useState(false);
  const [points, setPoints] = useState(0);
  const [frame, setFrame] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [pose, setPose] = useState<PoseReadout | null>(null);

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xf7f9fc);
    const camera = new THREE.PerspectiveCamera(55, 1, 0.01, 1000);
    camera.position.set(2.4, 1.9, 2.4);
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    element.appendChild(renderer.domElement);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    const grid = new THREE.GridHelper(20, 20, 0xaabbd3, 0xd9e1ec);
    const axes = new THREE.AxesHelper(0.7);
    scene.add(grid, axes);
    const marker = new THREE.Group();
    const markerGeometry = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(0.25, 0, 0), new THREE.Vector3(-0.14, 0, 0.12),
      new THREE.Vector3(-0.14, 0, 0.12), new THREE.Vector3(-0.14, 0, -0.12),
      new THREE.Vector3(-0.14, 0, -0.12), new THREE.Vector3(0.25, 0, 0),
    ]);
    marker.add(new THREE.LineSegments(markerGeometry, new THREE.LineBasicMaterial({ color: 0x1765ff })));
    const markerAxes = new THREE.AxesHelper(0.35);
    marker.add(markerAxes);
    marker.visible = false;
    scene.add(marker);
    const pathMaterial = new THREE.LineBasicMaterial({ color: 0x1765ff });
    let paths: PathChunk[] = [];
    let active: PathChunk | null = null;
    let lastPosition: THREE.Vector3 | null = null;
    let pointCount = 0;
    let lastSequence = 0;
    let latest: THREE.Vector3 | null = null;
    let lastPoseAt = 0;
    let socket: WebSocket | null = null;
    let reconnect: ReturnType<typeof setTimeout> | undefined;
    let animation = 0;
    let alive = true;

    function clearTrajectory() {
      for (const chunk of paths) {
        scene.remove(chunk.line);
        chunk.line.geometry.dispose();
      }
      paths = [];
      active = null;
      lastPosition = null;
      latest = null;
      pointCount = 0;
      lastSequence = 0;
      marker.visible = false;
      setPoints(0);
      setPose(null);
    }

    function appendPoint(point: Point) {
      if (point.seq <= lastSequence) return;
      const position = toView(point.position);
      const previous = active && active.segment === point.segment ? lastPosition : null;
      if (!active || active.segment !== point.segment || active.count >= CHUNK_POINTS) {
        const positions = new Float32Array(CHUNK_POINTS * 3);
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3).setUsage(THREE.DynamicDrawUsage));
        geometry.setDrawRange(0, 0);
        const line = new THREE.Line(geometry, pathMaterial);
        line.frustumCulled = false;
        scene.add(line);
        active = { line, positions, count: 0, segment: point.segment };
        paths.push(active);
        if (previous) {
          positions.set(previous.toArray(), 0);
          active.count = 1;
        }
      }
      const offset = active.count * 3;
      active.positions.set(position.toArray(), offset);
      active.count += 1;
      const attribute = active.line.geometry.getAttribute("position") as THREE.BufferAttribute;
      attribute.needsUpdate = true;
      active.line.geometry.setDrawRange(0, active.count);
      lastPosition = position;
      lastSequence = point.seq;
      pointCount += 1;
    }

    function updateLatest(point: Point) {
      latest = toView(point.position);
      marker.position.copy(latest);
      const quaternion = new THREE.Quaternion(...point.orientation as [number, number, number, number]).normalize();
      marker.quaternion.copy(basis).multiply(quaternion).multiply(basisInverse);
      marker.visible = true;
      const angles = new THREE.Euler().setFromQuaternion(quaternion, "ZYX");
      setPose({ position: point.position, rpy: [angles.x, angles.y, angles.z].map(THREE.MathUtils.radToDeg) });
    }

    function connect() {
      socket = new WebSocket(
        `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/vio/trajectory`
      );
      socket.onopen = () => setConnected(true);
      socket.onmessage = (event) => {
        const packet = JSON.parse(event.data) as Packet;
        if (packet.reset) clearTrajectory();
        for (const point of packet.points) appendPoint(point);
        if (packet.points.length) {
          updateLatest(packet.points[packet.points.length - 1]);
          setPoints(pointCount);
          lastPoseAt = performance.now();
        }
        setTracking(packet.tracking);
        setFrame(packet.frame);
      };
      socket.onclose = () => {
        setConnected(false);
        setTracking(false);
        if (alive) reconnect = setTimeout(connect, 1500);
      };
      socket.onerror = () => socket?.close();
    }

    const resize = () => {
      const width = Math.max(1, element.clientWidth);
      const height = Math.max(1, element.clientHeight);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height, false);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    resize();
    const render = () => {
      animation = requestAnimationFrame(render);
      if (latest && follow.current) {
        const shift = latest.clone().sub(controls.target).multiplyScalar(0.08);
        controls.target.add(shift);
        camera.position.add(shift);
      }
      controls.update();
      renderer.render(scene, camera);
    };
    render();
    const staleTimer = setInterval(() => setStale(!lastPoseAt || performance.now() - lastPoseAt > 2000), 500);
    connect();
    return () => {
      alive = false;
      clearInterval(staleTimer);
      clearTimeout(reconnect);
      socket?.close();
      cancelAnimationFrame(animation);
      observer.disconnect();
      controls.dispose();
      for (const chunk of paths) chunk.line.geometry.dispose();
      pathMaterial.dispose();
      markerGeometry.dispose();
      ((marker.children[0] as THREE.LineSegments).material as THREE.Material).dispose();
      for (const helper of [grid, axes, markerAxes]) {
        helper.geometry.dispose();
        const materials = Array.isArray(helper.material) ? helper.material : [helper.material];
        for (const material of materials) material.dispose();
      }
      renderer.dispose();
      element.removeChild(renderer.domElement);
    };
  }, []);

  const status = !connected ? "连接可视化服务中" : tracking && !stale ? "跟踪中" : points ? "跟踪中断" : "等待位姿";
  return <section className="panel vio-trajectory-panel">
    <div className="panel-heading">
      <div><p className="eyebrow">LIVE TRAJECTORY</p><h3>VIO 轨迹</h3></div>
      <span className={`badge ${tracking && !stale ? "good" : "neutral"}`}>{status}</span>
    </div>
    <div className="vio-trajectory-stage" ref={host}>
      <div className="vio-trajectory-overlay">
        <span>设备 CPU <strong>{cpuPercent == null ? "—" : `${cpuPercent.toFixed(1)}%`}</strong></span>
        <span>轨迹点 <strong>{points}</strong></span>
        <span>坐标系 <strong>{frame || "—"}</strong></span>
      </div>
      <div className="vio-pose-readout">
        <span>位置 · m</span>
        <div>{["X", "Y", "Z"].map((axis, index) => <label key={axis}>{axis}<strong>{pose ? pose.position[index].toFixed(3) : "—"}</strong></label>)}</div>
        <span>姿态 · ° · ZYX</span>
        <div>{["Roll", "Pitch", "Yaw"].map((axis, index) => <label key={axis}>{axis}<strong>{pose ? pose.rpy[index].toFixed(1) : "—"}</strong></label>)}</div>
      </div>
    </div>
    <div className="vio-trajectory-footer">
      <span>拖动旋转 · 滚轮缩放 · 右键平移</span>
      <div className="vio-trajectory-actions">
        {points > 0 && <a className="button light" href="/api/vio/trajectory/download">下载完整轨迹</a>}
        <button className="button light" onClick={() => {
          follow.current = !follow.current;
          setFollowing(follow.current);
        }}>{following ? "关闭跟随" : "跟随设备"}</button>
      </div>
    </div>
    {rosError && <p className="vio-trajectory-error inline-error">{rosError}</p>}
  </section>;
}
