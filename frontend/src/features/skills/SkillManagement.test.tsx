import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import SkillManagement from "./SkillManagement";

const response = (skills: unknown[] = []) => ({ ok: true, status: 200, json: async () => ({ skills }) });
const demo = { name: "demo", agents: ["supervisor", "data-analyst"] };
const props = { apiUrl: "http://test", token: "user-a", onBack: vi.fn(), onExpired: vi.fn() };
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.clearAllMocks(); });

it("合并列表显示所属 Agent，取消不请求，确认后刷新空列表", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response([demo])).mockResolvedValueOnce(response()).mockResolvedValueOnce(response());
  vi.stubGlobal("fetch", fetcher);
  render(<SkillManagement {...props} />);
  expect(screen.getByText("正在加载私有 Skill…")).toBeTruthy();
  expect(await screen.findByText("demo")).toBeTruthy();
  expect(screen.getByText("所属 Agent：supervisor、data-analyst")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "删除 Skill demo" }));
  expect(screen.getByText(/将删除所有 Agent 下的私有副本/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(fetcher).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "删除 Skill demo" }));
  fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
  expect(await screen.findByText("暂无私有 Skill")).toBeTruthy();
  expect(fetcher.mock.calls[1][0]).toBe("http://test/skills/user/demo");
  expect(fetcher.mock.calls[1][1]).toMatchObject({ method: "DELETE", headers: { Authorization: "Bearer user-a" } });
  fireEvent.click(screen.getByRole("button", { name: "返回设置" }));
  expect(props.onBack).toHaveBeenCalledOnce();
});

it("加载失败可重试", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new Error("读取失败")).mockResolvedValueOnce(response()));
  render(<SkillManagement {...props} />);
  expect(await screen.findByText("读取失败")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByText("暂无私有 Skill")).toBeTruthy();
});

it("删除中防止重复提交，失败后保留列表和重试入口", async () => {
  let reject!: (reason: Error) => void;
  const fetcher = vi.fn().mockResolvedValueOnce(response([demo])).mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
  vi.stubGlobal("fetch", fetcher);
  render(<SkillManagement {...props} />);
  await screen.findByText("demo");
  fireEvent.click(screen.getByRole("button", { name: "删除 Skill demo" }));
  fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
  const pending = screen.getByRole("button", { name: "删除中…" }) as HTMLButtonElement;
  expect(pending.disabled).toBe(true);
  fireEvent.click(pending);
  expect(fetcher).toHaveBeenCalledTimes(2);
  await act(async () => reject(new Error("删除失败")));
  expect(screen.getByText("删除失败")).toBeTruthy();
  expect(screen.getByText("demo")).toBeTruthy();
  expect((screen.getByRole("button", { name: "确认删除" }) as HTMLButtonElement).disabled).toBe(false);
});

it("切换身份及关闭页面取消请求，迟到结果不覆盖新列表", async () => {
  let resolve!: (value: unknown) => void;
  const fetcher = vi.fn().mockImplementationOnce(() => new Promise(done => { resolve = done; })).mockResolvedValueOnce(response());
  vi.stubGlobal("fetch", fetcher);
  const view = render(<SkillManagement {...props} />);
  const signal = fetcher.mock.calls[0][1].signal as AbortSignal;
  view.rerender(<SkillManagement {...props} token="user-b" />);
  expect(signal.aborted).toBe(true);
  await screen.findByText("暂无私有 Skill");
  await act(async () => resolve(response([demo])));
  expect(screen.queryByText("demo")).toBeNull();
  view.unmount();
  expect(fetcher.mock.calls[1][1].signal.aborted).toBe(true);
});

it("认证失效交给应用处理", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ status: 401 }));
  render(<SkillManagement {...props} />);
  await waitFor(() => expect(props.onExpired).toHaveBeenCalledOnce());
});
