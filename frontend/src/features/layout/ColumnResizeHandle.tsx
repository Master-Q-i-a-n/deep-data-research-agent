import { KeyboardEvent, PointerEvent, useEffect, useLayoutEffect, useRef, useState } from "react";
import "./column-resize-handle.css";

type Props = {
  label: string;
  defaultWidth: number;
  minWidth: number;
  maxWidth: number;
  remainingWidth: number;
  onResize: (width: number | undefined) => void;
};

// 两个侧栏共用拖动行为；宽度由各自的布局维护，不影响内部滚动。
export default function ColumnResizeHandle({ label, defaultWidth, minWidth, maxWidth, remainingWidth, onResize }: Props) {
  const handleRef = useRef<HTMLDivElement>(null);
  const [measuredWidth, setMeasuredWidth] = useState(defaultWidth);
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef<{
    pointerId: number; startX: number; startWidth: number;
    cursor: string; userSelect: string;
  } | null>(null);

  useLayoutEffect(() => {
    const panel = handleRef.current?.parentElement;
    if (!panel) return;
    const measure = () => {
      const width = panel.getBoundingClientRect().width;
      if (width > 0) setMeasuredWidth(Math.round(width));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(panel);
    return () => observer.disconnect();
  }, []);

  function restoreDragStyles() {
    const drag = dragRef.current;
    if (!drag) return;
    dragRef.current = null;
    document.body.style.cursor = drag.cursor;
    document.body.style.userSelect = drag.userSelect;
  }

  useEffect(() => () => restoreDragStyles(), []);

  function limitWidth(width: number) {
    const available = handleRef.current?.parentElement?.parentElement?.getBoundingClientRect().width;
    // 留出内容区的可用宽度，避免侧栏扩展后把查询结果挤到窗口外。
    const upper = available ? Math.max(minWidth, Math.min(maxWidth, available - remainingWidth)) : maxWidth;
    return Math.round(Math.max(minWidth, Math.min(upper, width)));
  }

  function resize(width: number) {
    const next = limitWidth(width);
    setMeasuredWidth(next);
    onResize(next);
  }

  function startDrag(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || dragRef.current) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId, startX: event.clientX,
      startWidth: event.currentTarget.parentElement!.getBoundingClientRect().width || measuredWidth,
      cursor: document.body.style.cursor, userSelect: document.body.style.userSelect,
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    setDragging(true);
  }

  function finishDrag(event: PointerEvent<HTMLDivElement>) {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    restoreDragStyles();
    setDragging(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }

  function resizeWithKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = event.currentTarget.parentElement!.getBoundingClientRect().width || measuredWidth;
    const step = event.shiftKey ? 32 : 16;
    resize(event.key === "Home" ? minWidth : event.key === "End" ? maxWidth : current + (event.key === "ArrowLeft" ? -step : step));
  }

  return <div
    ref={handleRef} className={`column-resize-handle${dragging ? " is-dragging" : ""}`}
    role="separator" aria-label={label} aria-orientation="vertical"
    aria-valuemin={minWidth} aria-valuemax={maxWidth} aria-valuenow={measuredWidth} tabIndex={0}
    title="拖动调整宽度，双击恢复默认"
    onPointerDown={startDrag}
    onPointerMove={event => {
      const drag = dragRef.current;
      if (drag?.pointerId === event.pointerId) resize(drag.startWidth + event.clientX - drag.startX);
    }}
    onPointerUp={finishDrag} onPointerCancel={finishDrag} onLostPointerCapture={finishDrag}
    onKeyDown={resizeWithKeyboard}
    onDoubleClick={() => { setMeasuredWidth(defaultWidth); onResize(undefined); }}
  />;
}
