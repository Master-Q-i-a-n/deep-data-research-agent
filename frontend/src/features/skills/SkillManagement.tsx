import { useEffect, useRef, useState } from "react";

type Skill = { name: string; agents: string[] };
type Props = { apiUrl: string; token: string; onBack: () => void; onExpired: () => void };

export default function SkillManagement({ apiUrl, token, onBack, onExpired }: Props) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirm, setConfirm] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [revision, setRevision] = useState(0);
  const deletion = useRef<AbortController | null>(null);

  useEffect(() => () => { deletion.current?.abort(); }, [token]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void (async () => {
      try {
        const response = await fetch(`${apiUrl}/skills/user`, {
          headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        if (response.status === 401) { onExpired(); return; }
        const body = await response.json();
        if (controller.signal.aborted) return;
        if (!response.ok) throw new Error(body.detail || "读取私有 Skill 失败");
        setSkills(body.skills);
      } catch (err) {
        if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "读取私有 Skill 失败");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    // 页面关闭、刷新或身份变化后，旧响应不得覆盖新页面。
    return () => controller.abort();
  }, [apiUrl, token, revision, onExpired]);

  async function remove() {
    if (!confirm || deletion.current) return;
    const name = confirm;
    const controller = new AbortController();
    deletion.current = controller;
    setDeleting(true);
    setActionError("");
    setNotice("");
    try {
      const response = await fetch(`${apiUrl}/skills/user/${encodeURIComponent(name)}`, {
        method: "DELETE", headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (response.status === 401) { onExpired(); return; }
      const body = await response.json();
      if (controller.signal.aborted) return;
      if (!response.ok) throw new Error(body.detail || "删除 Skill 失败，请稍后重试");
      setConfirm(null);
      setNotice(`已删除 ${name} 的全部私有副本。`);
      setRevision(value => value + 1);
    } catch (err) {
      if (!controller.signal.aborted) setActionError(err instanceof Error ? err.message : "删除 Skill 失败，请稍后重试");
    } finally {
      if (!controller.signal.aborted) {
        deletion.current = null;
        setDeleting(false);
      }
    }
  }

  return <div className="skill-management">
    <button type="button" className="settings-action-button" onClick={onBack}>返回设置</button>
    <p className="settings-hint">仅显示你的私有 Skill。删除从下一次 Agent 执行起生效，运行中的任务可能仍使用已加载内容；同名公共 Skill 将恢复使用。</p>
    {notice ? <p className="settings-status settings-status--success" role="status">{notice}</p> : null}
    {loading ? <p role="status">正在加载私有 Skill…</p> : error ? <div role="alert">
      <p>{error}</p><button type="button" className="settings-action-button" onClick={() => setRevision(value => value + 1)}>重试</button>
    </div> : skills.length === 0 ? <p>暂无私有 Skill</p> : <ul className="skill-management__list">
      {skills.map(skill => <li key={skill.name} className="settings-section">
        <div className="settings-section__copy"><strong>{skill.name}</strong><span>所属 Agent：{skill.agents.join("、")}</span></div>
        <button type="button" className="settings-danger-button" disabled={deleting}
          aria-label={`删除 Skill ${skill.name}`} onClick={() => { setConfirm(skill.name); setActionError(""); setNotice(""); }}>删除</button>
      </li>)}
    </ul>}
    {confirm ? <div className="settings-confirm" role="alert">
      <p>确认删除「{confirm}」？将删除所有 Agent 下的私有副本，此操作无法撤销。</p>
      <div><button type="button" disabled={deleting} onClick={() => { setConfirm(null); setActionError(""); }}>取消</button>
        <button type="button" disabled={deleting} onClick={() => void remove()}>{deleting ? "删除中…" : "确认删除"}</button></div>
      {actionError ? <p className="settings-status settings-status--error">{actionError}</p> : null}
    </div> : null}
  </div>;
}
