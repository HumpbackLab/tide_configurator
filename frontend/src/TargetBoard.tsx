import { useEffect, useRef, useState } from "react";

export type TargetSpec = {
  cols: number;
  rows: number;
  spacing: number;
  span_tags: number;
  view_width: number;
  view_height: number;
  span_start: number;
  span_end: number;
};

export function TargetBoard({
  spec,
  recording,
  onScale,
}: {
  spec: TargetSpec;
  recording: boolean;
  onScale: (pixels: number) => void;
}) {
  const image = useRef<HTMLImageElement>(null);
  const report = useRef(onScale);
  report.current = onScale;
  const [lockedWidth, setLockedWidth] = useState<number>();
  useEffect(() => {
    const element = image.current!;
    // Keep the pattern at the same pixel size throughout a recording, even if
    // surrounding controls grow. Page zoom still requires a fresh measurement.
    setLockedWidth(
      recording ? element.getBoundingClientRect().width : undefined,
    );
  }, [recording]);
  useEffect(() => {
    const element = image.current!;
    const measure = () => {
      const width = element.getBoundingClientRect().width;
      if (width > 0) report.current(width * window.devicePixelRatio);
    };
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);
  return (
    <div className="target-body">
      <div
        className="target-pattern"
        style={
          lockedWidth ? { width: lockedWidth, maxWidth: "none" } : undefined
        }
      >
        <img
          ref={image}
          src="/api/target.svg"
          width={spec.view_width}
          height={spec.view_height}
          alt={`${spec.cols} 列 ${spec.rows} 行 AprilGrid 标定板，测量左右最外侧黑色小方块的外缘跨度`}
          draggable={false}
        />
        <svg
          className="target-dimension"
          viewBox={`0 0 ${spec.view_width} 44`}
          aria-label="测量左端黑色小方块左外缘到右端黑色小方块右外缘的水平距离"
        >
          <path
            d={`M${spec.span_start} 0v23m0 -8H${spec.span_end}m0 -15v23 M${spec.span_start + 8} 11l-8 4 8 4 M${spec.span_end - 8} 11l8 4 -8 4`}
          />
          <text x={spec.view_width / 2} y="37" textAnchor="middle">
            左外缘 ← 实测跨度 → 右外缘
          </text>
        </svg>
      </div>
    </div>
  );
}
