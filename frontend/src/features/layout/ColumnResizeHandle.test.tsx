import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import ColumnResizeHandle from "./ColumnResizeHandle";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("拖动越过交界仍然调整宽度，为内容区保留空间，取消后恢复光标和文本选择", () => {
  const onResize = vi.fn();
  const view = render(<div><aside><ColumnResizeHandle label="侧栏宽度" defaultWidth={250} minWidth={220} maxWidth={380} remainingWidth={560} onResize={onResize} /></aside></div>);
  const handle = screen.getByRole("separator", { name: "侧栏宽度" });
  vi.spyOn(handle.parentElement!, "getBoundingClientRect").mockReturnValue({ width: 250 } as DOMRect);
  vi.spyOn(handle.parentElement!.parentElement!, "getBoundingClientRect").mockReturnValue({ width: 900 } as DOMRect);
  const capture = vi.fn();
  const release = vi.fn();
  Object.assign(handle, { setPointerCapture: capture, hasPointerCapture: () => true, releasePointerCapture: release });
  const previousCursor = document.body.style.cursor;
  const previousSelect = document.body.style.userSelect;

  fireEvent.pointerDown(handle, { pointerId: 1, button: 0, clientX: 250 });
  expect(capture).toHaveBeenCalledWith(1);
  expect(document.body.style.cursor).toBe("col-resize");
  expect(document.body.style.userSelect).toBe("none");
  fireEvent.pointerMove(handle, { pointerId: 2, clientX: 500 });
  expect(onResize).not.toHaveBeenCalled();
  fireEvent.pointerMove(handle, { pointerId: 1, clientX: 500 });
  expect(onResize).toHaveBeenLastCalledWith(340);
  fireEvent.pointerMove(handle, { pointerId: 1, clientX: -100 });
  expect(onResize).toHaveBeenLastCalledWith(220);
  fireEvent.pointerCancel(handle, { pointerId: 1 });
  expect(release).toHaveBeenCalledWith(1);
  expect(document.body.style.cursor).toBe(previousCursor);
  expect(document.body.style.userSelect).toBe(previousSelect);

  fireEvent.pointerDown(handle, { pointerId: 3, button: 0, clientX: 250 });
  view.unmount();
  expect(document.body.style.cursor).toBe(previousCursor);
  expect(document.body.style.userSelect).toBe(previousSelect);
});

it("左右方向键可调整宽度，双击恢复默认", () => {
  const onResize = vi.fn();
  render(<aside><ColumnResizeHandle label="侧栏宽度" defaultWidth={250} minWidth={220} maxWidth={380} remainingWidth={560} onResize={onResize} /></aside>);
  const handle = screen.getByRole("separator", { name: "侧栏宽度" });
  fireEvent.keyDown(handle, { key: "ArrowRight" });
  expect(onResize).toHaveBeenLastCalledWith(266);
  fireEvent.keyDown(handle, { key: "ArrowLeft", shiftKey: true });
  expect(onResize).toHaveBeenLastCalledWith(234);
  fireEvent.doubleClick(handle);
  expect(onResize).toHaveBeenLastCalledWith(undefined);
});
