"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { API_URL, api, canEdit, editLockReason, errorMessage, isBusy, Project, statusLabel, StoryEvent } from "@/lib/api";
import ChapterEditor from "./chapter-editor";

const tabs = [{ id: "bible", label: "Thế giới & nhân vật" }, { id: "outline", label: "Dàn ý" }, { id: "chapters", label: "Các chương" }, { id: "activity", label: "Nhật ký" }] as const;
const stages = ["Ý tưởng", "Thế giới", "Nhân vật", "Dàn ý", "Viết & biên tập", "Hoàn thành"];
const stageLabel = (s?: string) => ({ analyze: "Phân tích ý tưởng", write: "Viết chương", revise: "Chỉnh sửa chương", memory: "Ghi nhớ diễn biến", final_review: "Duyệt toàn bộ tác phẩm", queued: "Đang chờ đến lượt", bible: "Xây dựng thế giới", characters: "Phát triển nhân vật", outline: "Lên dàn ý", chapters: "Viết & biên tập các chương", writing: "Đang viết bản thảo", review: "Đang biên tập", completed: "Đã hoàn thành", done: "Đã hoàn thành" }[(s || "").split(":")[0]] || s || "Chuẩn bị sáng tác");
export default function ProjectDetail({ id, onUpdate }: { id: string; onUpdate: () => Promise<void> }) {
  const [project, setProject] = useState<Project | null>(null);
  const [error, setError] = useState(""); const [loaded, setLoaded] = useState(false);
  const [tab, setTab] = useState<typeof tabs[number]["id"]>("chapters");
  const [events, setEvents] = useState<StoryEvent[]>([]);
  const [connection, setConnection] = useState("Đang kết nối");
  const [action, setAction] = useState("");
  const [editorDirty, setEditorDirty] = useState(false);
  const request = useRef(0);
  const refresh = useCallback(async () => {
    const seq = ++request.current;
    try { const data = await api<Project>(`/projects/${encodeURIComponent(id)}`); if (request.current !== seq) return; setProject(data); setError(""); if (data.events?.length) setEvents(previous => mergeEvents(previous, data.events!)); }
    catch (e) { if (request.current === seq) setError(errorMessage(e)); }
    finally { if (request.current === seq) setLoaded(true); }
  }, [id]);
  useEffect(() => {
    void refresh();
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    const stream = new EventSource(`${API_URL}/api/projects/${encodeURIComponent(id)}/events`);
    stream.onopen = () => { setConnection("Đã kết nối trực tiếp"); void refresh(); };
    stream.onmessage = event => { try { const data = JSON.parse(event.data) as StoryEvent; if (!data.type) return; setEvents(previous => mergeEvents(previous, [data])); if (!refreshTimer) refreshTimer = setTimeout(() => { refreshTimer = undefined; void refresh(); void onUpdate(); }, 350); } catch { /* Ignore keepalive/non-JSON frames. */ } };
    stream.onerror = () => setConnection("Đang nối lại · tự làm mới mỗi 5 giây");
    const poll = setInterval(() => { void refresh(); }, 5000);
    return () => { stream.close(); clearInterval(poll); if (refreshTimer) clearTimeout(refreshTimer); request.current++; };
  }, [id, refresh, onUpdate]);
  async function runAction(kind: "run" | "cancel") {
    if (kind === "run" && project?.chapters?.length && !window.confirm("Chạy lại có thể viết lại các chương hiện có. Bản sửa của bạn đã được lưu trong lịch sử. Tiếp tục?")) return;
    setAction(kind); setError("");
    try { await api(`/projects/${encodeURIComponent(id)}/${kind}`, { method: "POST" }); await refresh(); await onUpdate(); }
    catch (e) { setError(errorMessage(e)); } finally { setAction(""); }
  }
  if (!loaded) return <div className="empty-state" role="status">Đang mở bản thảo…</div>;
  if (!project) return <div className="empty-state"><h2>Chưa thể mở tác phẩm</h2><p className="error" role="alert">{error}</p><button className="button secondary" onClick={() => void refresh()}>Thử lại</button> <Link href="/">Về thư viện</Link></div>;
  const busy = isBusy(project.status) || isBusy(project.job?.status);
  const chapters = [...(project.chapters || [])].sort((a, b) => a.chapter_number - b.chapter_number);
  const complete = ["completed", "done", "complete", "edited", "needs_review"].includes(project.status);
  const chapterCount = project.options.chapter_count ?? project.spec?.chapter_count ?? project.outline?.length ?? 0;
  const targetWords = project.options.target_words ?? project.spec?.target_words;
  const progress = complete ? 100 : Math.min(95, Math.round((chapters.filter(c => c.content).length / Math.max(1, chapterCount)) * 80) + (project.bible ? 5 : 0) + (project.characters?.length ? 5 : 0) + (project.outline?.length ? 5 : 0));
  const currentStage = complete ? 5 : chapters.length ? 4 : project.outline?.length ? 4 : project.characters?.length ? 3 : project.bible ? 2 : busy ? 1 : 0;
  return <>
    <Link href="/" className="back-link">← Thư viện truyện</Link>
    <div className="page-heading detail-heading"><div><div className="eyebrow">BẢN THẢO CỦA BẠN</div><h1>{project.title}</h1><div className="detail-meta"><span className={`status ${project.status}`}>{statusLabel(project.status)}</span><span>{chapterCount || "Tự suy luận"} chương</span><span>~{targetWords?.toLocaleString("vi-VN") || "tự suy luận"} từ / chương</span></div></div><div className="action-group">{chapters.length > 0 && <a className="button secondary" href={`${API_URL}/api/projects/${encodeURIComponent(id)}/export`} target="_blank" rel="noreferrer">↓ Xuất Markdown</a>}{busy ? <button className="button danger" disabled={!!action} onClick={() => void runAction("cancel")}>{action ? "Đang xử lý…" : "■ Dừng viết"}</button> : <button className="button primary" disabled={!!action} onClick={() => void runAction("run")}>{action ? "Đang xử lý…" : "↻ Chạy lại"}</button>}</div></div>
    {error && <div className="error" role="alert">{error}</div>}
    {project.job?.error && <div className="error" role="alert"><strong>Lần chạy gặp lỗi:</strong> {project.job.error}<p>Bạn có thể chạy lại sau khi kiểm tra cấu hình máy chủ.</p></div>}
    <section className="progress-card"><div className="progress-heading"><div><span className={`dot ${busy ? "pulse" : ""}`} /><strong>{busy ? stageLabel(project.job?.stage) : statusLabel(project.status)}</strong></div><span>{chapters.filter(c => c.content).length}/{chapterCount || "Tự suy luận"} chương · {progress}%</span></div><div className="progress-track" role="progressbar" aria-label="Tiến độ sáng tác ước tính" aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100}><div style={{ width: `${progress}%` }} /></div><ol className="stage-list">{stages.map((stage, i) => <li key={stage} className={i <= currentStage ? "reached" : ""}><span>{i < currentStage ? "✓" : i + 1}</span>{stage}</li>)}</ol><div className="connection-label">{connection} · Tiến độ ước tính</div></section>
    <details className="prompt-details"><summary>Ý tưởng gốc</summary><p>{project.prompt}</p></details>
    <div className="tabs" role="tablist" aria-label="Nội dung tác phẩm">{tabs.map(item => <button key={item.id} role="tab" id={`tab-${item.id}`} aria-controls={`panel-${item.id}`} aria-selected={tab === item.id} className={tab === item.id ? "active" : ""} onClick={() => { if (item.id === tab) return; if (editorDirty && !window.confirm("Bạn có thay đổi chưa lưu. Bỏ thay đổi để chuyển thẻ?")) return; setEditorDirty(false); setTab(item.id); }}>{item.label}{item.id === "chapters" && <span>{chapters.length}</span>}</button>)}</div>
    <section role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
      {tab === "bible" && <>{project.bible ? <div className="content-card"><div className="eyebrow">STORY BIBLE</div><h2>Thế giới của câu chuyện</h2><ObjectView value={project.bible} /></div> : <Empty text="Thế giới câu chuyện sẽ xuất hiện ở đây sau bước xây dựng bối cảnh." />}<div className="section-heading"><h2>Dàn nhân vật</h2><span className="muted">{project.characters?.length || 0} nhân vật</span></div><div className="character-grid">{project.characters?.map((c, i) => <article className="content-card character-card" key={`${c.name}-${i}`}><span className="character-avatar">{c.name?.charAt(0)}</span><h3>{c.name}</h3><span className="tag subtle">{c.role}</span><p>{c.description}</p>{c.arc != null && <div className="character-arc"><strong>Hành trình nhân vật</strong><ObjectView value={c.arc} /></div>}</article>)}</div></>}
      {tab === "outline" && (project.outline?.length ? <div className="outline-list">{project.outline.map(o => <article className="content-card outline-card" key={o.chapter_number}><span className="outline-number">{String(o.chapter_number).padStart(2, "0")}</span><div><h3>{o.title}</h3><p>{o.summary}</p>{o.beats?.length > 0 && <ul>{o.beats.map((b, i) => <li key={i}><ObjectView value={b} /></li>)}</ul>}</div></article>)}</div> : <Empty text="Dàn ý từng chương đang chờ được xây dựng." />)}
      {tab === "chapters" && (chapters.length ? <ChapterEditor projectId={id} chapters={chapters} busy={!canEdit(project)} lockReason={editLockReason(project)} onDirtyChange={setEditorDirty} onSaved={refresh} /> : <Empty text={busy ? "AI đang chuẩn bị câu chuyện. Chương đầu tiên sẽ xuất hiện tại đây khi có bản thảo." : "Chưa có chương nào. Bắt đầu hoặc chạy lại để sáng tác."} />)}
      {tab === "activity" && <div className="content-card"><div className="section-heading"><h2>Nhật ký sáng tác</h2><span className="muted">{events.length} sự kiện gần nhất</span></div>{events.length ? <div className="event-list">{[...events].reverse().map((e, i) => <details key={`${e.id}-${i}`}><summary><span className="event-index">#{e.id}</span><strong>{eventLabel(e.type)}</strong><span className="muted">{String(e.payload?.stage || e.payload?.status || "")}</span></summary><ObjectView value={e.payload} /></details>)}</div> : <Empty text="Chưa có sự kiện. Nhật ký sẽ tự cập nhật trong quá trình viết." />}</div>}
    </section>
  </>;
}
function mergeEvents(previous: StoryEvent[], incoming: StoryEvent[]) { const map = new Map(previous.map(e => [String(e.id), e])); for (const e of incoming) map.set(String(e.id), e); return [...map.values()].slice(-200); }
function eventLabel(type: string) { return ({ queued: "Trong hàng đợi", stage_started: "Bắt đầu giai đoạn", stage_completed: "Hoàn thành giai đoạn", job_done: "Hoàn thành sáng tác", chapter_done: "Hoàn thành chương", chapter_edited: "Đã sửa chương", project_created: "Đã tạo tác phẩm", job_started: "Bắt đầu sáng tác", job_completed: "Hoàn thành sáng tác", job_failed: "Quá trình gặp lỗi", chapter_completed: "Hoàn thành chương", chapter_saved: "Đã lưu chương", chapter_updated: "Đã cập nhật chương", job_cancelled: "Đã dừng sáng tác", stage_changed: "Chuyển giai đoạn", bible_created: "Đã dựng thế giới", outline_created: "Đã tạo dàn ý" }[type] || type.replaceAll("_", " ")); }
export function Empty({ text }: { text: string }) { return <div className="empty-state"><span className="empty-icon">✧</span><p>{text}</p></div>; }
const fieldNames: Record<string, string> = { start: "Khởi đầu", end: "Kết thúc", world: "Thế giới", rule: "Quy tắc", facts: "Sự kiện", characters: "Nhân vật", premise: "Tiền đề", title: "Tên truyện", genre: "Thể loại", setting: "Bối cảnh", tone: "Giọng văn", themes: "Chủ đề", theme: "Chủ đề", rules: "Quy tắc thế giới", world_rules: "Quy tắc thế giới", conflict: "Xung đột", central_conflict: "Xung đột trung tâm", synopsis: "Tóm tắt", style: "Phong cách", pov: "Ngôi kể", language: "Ngôn ngữ", target_audience: "Độc giả", summary: "Tóm tắt", description: "Mô tả", name: "Tên", role: "Vai trò", arc: "Hành trình", feedback: "Nhận xét", score: "Điểm", issues: "Điểm cần sửa", strengths: "Điểm mạnh" };
export function ObjectView({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="muted">—</span>;
  if (typeof value === "object") return Array.isArray(value) ? <ul className="object-list">{value.map((v, i) => <li key={i}><ObjectView value={v} /></li>)}</ul> : <dl className="object-view">{Object.entries(value).map(([k, v]) => <div key={k}><dt>{fieldNames[k] || k.replaceAll("_", " ")}</dt><dd><ObjectView value={v} /></dd></div>)}</dl>;
  return <span className="preserve-lines">{String(value)}</span>;
}
