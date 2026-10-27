"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, dateLabel, errorMessage, isBusy, Project, statusLabel } from "@/lib/api";
import AppShell from "./app-shell";
import ProjectDetail from "./project-detail";

export default function Dashboard({ projectId }: { projectId?: string }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const router = useRouter();
  const refresh = useCallback(async () => {
    try { setProjects(await api<Project[]>("/projects")); setError(""); }
    catch (e) { setError(errorMessage(e)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 15000);
    return () => clearInterval(timer);
  }, [refresh]);
  return <AppShell crumb={projectId ? "Bản thảo" : "Thư viện"} active={projectId ? undefined : "library"} onCreateNew={() => setCreating(true)}>
    {projectId ? <ProjectDetail key={projectId} id={projectId} onUpdate={refresh} /> : <>
      <div className="page-heading"><div><div className="eyebrow">TỪ MỘT Ý TƯỞNG, ĐẾN MỘT THẾ GIỚI</div><h1>Thư viện của bạn<span className="accent">.</span></h1><p>Mỗi câu chuyện hay đều bắt đầu từ một trang trắng.</p></div><button className="button primary" onClick={() => setCreating(true)}>＋ Tạo truyện mới</button></div>
      <section className="welcome-card"><div><span className="tag">NGƯỜI BẠN ĐỒNG SÁNG TÁC</span><h2>Bạn tưởng tượng.<br /><em>AI cùng bạn viết.</em></h2><p>Từ thế giới, nhân vật đến từng chương truyện.<br />Theo dõi quá trình, chỉnh sửa và làm nên dấu ấn riêng.</p><button className="button light" onClick={() => setCreating(true)}>Bắt đầu một câu chuyện <span>↗</span></button></div><div className="book-art" aria-hidden="true"><div className="art-orbit" /><span className="art-star">✧</span><div className="book back-book" /><div className="book front-book"><span>TRUYỆN<br />CHƯA KỂ</span><i>✦</i><small>MỘT THẾ GIỚI CỦA RIÊNG BẠN</small></div><span className="art-label">ý tưởng → câu chuyện</span></div></section>
      <section className="stats" aria-label="Thống kê thư viện"><div><span>Tổng tác phẩm</span><strong>{projects.length.toString().padStart(2, "0")}</strong></div><div><span>Đang sáng tác</span><strong>{projects.filter(p => isBusy(p.status)).length.toString().padStart(2, "0")}</strong></div><div><span>Đã hoàn thành</span><strong>{projects.filter(p => ["completed", "complete", "done"].includes(p.status)).length.toString().padStart(2, "0")}</strong></div></section>
      <div className="section-heading"><h2>Tác phẩm gần đây</h2><button className="text-button" onClick={() => { setLoading(true); void refresh(); }}>↻ Làm mới</button></div>
      {error && <div className="error" role="alert">{error}</div>}
      {loading ? <div className="empty-state" role="status">Đang tải thư viện…</div> : projects.length === 0 ? <div className="empty-state"><span className="empty-icon">▤</span><h3>Chưa có câu chuyện nào</h3><p>Hãy gieo ý tưởng đầu tiên. Phần còn lại, chúng ta cùng viết.</p><button className="button secondary" onClick={() => setCreating(true)}>＋ Tạo truyện đầu tiên</button></div> : <div className="project-grid">{projects.map((p, index) => <Link className="project-card" href={`/projects/${p.id}`} key={p.id}><div className={`project-cover cover-${index % 4}`}><span>TRUYỆN / {String(index + 1).padStart(2, "0")}</span><b>{p.title.charAt(0)}</b><i>✧</i></div><div className="project-card-body"><span className={`status ${p.status}`}>{statusLabel(p.status)}</span><h3>{p.title}</h3><p>{p.prompt}</p><div className="card-meta"><span>{(p.options?.mode ?? p.spec?.mode) === "short" ? "Truyện ngắn" : `${p.options?.chapter_count ?? p.spec?.chapter_count ?? "—"} chương`}</span><span>{dateLabel(p.created_at)}</span></div></div></Link>)}</div>}
    </>
  }
    {creating && <CreateProject onClose={() => setCreating(false)} onCreated={p => { setCreating(false); void refresh(); router.push(`/projects/${p.id}`); }} />}
  </AppShell>;
}

function CreateProject({ onClose, onCreated }: { onClose: () => void; onCreated: (p: Project) => void }) {
  const [title, setTitle] = useState(""); const [prompt, setPrompt] = useState("");
  const [storyMode, setStoryMode] = useState<"short" | "long">("long");
  const [chapterCount, setChapterCount] = useState<number | "">("");
  const [targetWords, setTargetWords] = useState<number | "">("");
  const [autoMode, setAutoMode] = useState(false);
  const [saving, setSaving] = useState(false); const [error, setError] = useState("");
  useEffect(() => { const handler = (e: KeyboardEvent) => { if (e.key === "Escape" && !saving) onClose(); }; document.addEventListener("keydown", handler); const previous = document.body.style.overflow; document.body.style.overflow = "hidden"; return () => { document.removeEventListener("keydown", handler); document.body.style.overflow = previous; }; }, [onClose, saving]);
  async function submit(e: FormEvent) { e.preventDefault(); setSaving(true); setError(""); try { const p = await api<Project>("/projects", { method: "POST", body: JSON.stringify({ title: title.trim(), prompt: prompt.trim(), options: { mode: storyMode, chapter_count: storyMode === "short" ? 1 : chapterCount === "" ? null : chapterCount, target_words: targetWords === "" ? null : targetWords, auto_mode: autoMode } }) }); onCreated(p); } catch (e) { setError(errorMessage(e)); setSaving(false); } }
  return <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="create-title"><div className="modal-heading"><div><div className="eyebrow">CHƯƠNG ĐẦU TIÊN LÀ Ý TƯỞNG</div><h2 id="create-title">Một câu chuyện mới</h2></div><button className="icon-button" aria-label="Đóng" disabled={saving} onClick={onClose}>×</button></div><form onSubmit={submit}>
    <label>Tên tác phẩm<input autoFocus required maxLength={200} value={title} onChange={e => setTitle(e.target.value)} placeholder="Ví dụ: Những lá thư từ Đà Lạt" disabled={saving} /></label>
    <label>Ý tưởng của bạn<textarea required minLength={10} maxLength={20000} rows={6} value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="Kể về nhân vật, bối cảnh, thể loại, giọng văn hoặc một tình huống bạn muốn khám phá…" disabled={saving} /><small>Càng cụ thể, câu chuyện càng gần với trí tưởng tượng của bạn.</small></label>
    <div className="form-row"><label>Thể loại phát hành<div className="mode-switch" role="radiogroup" aria-label="Thể loại phát hành"><button type="button" role="radio" aria-checked={storyMode === "short"} className={storyMode === "short" ? "mode-option selected" : "mode-option"} onClick={() => setStoryMode("short")} disabled={saving}>Truyện ngắn<small>Một chương duy nhất</small></button><button type="button" role="radio" aria-checked={storyMode === "long"} className={storyMode === "long" ? "mode-option selected" : "mode-option"} onClick={() => setStoryMode("long")} disabled={saving}>Truyện dài<small>Nhiều chương</small></button></div><small>Truyện ngắn luôn gồm đúng một chương.</small></label><label className={storyMode === "short" ? "disabled-field" : ""}>Số chương<input type="number" placeholder="Tự suy luận" min={1} max={30} value={storyMode === "short" ? 1 : chapterCount} onChange={e => setChapterCount(e.target.value === "" ? "" : Number(e.target.value))} disabled={saving || storyMode === "short"} /><small>{storyMode === "short" ? "Truyện ngắn = 1 chương" : "Để trống: suy luận từ ý tưởng · 1–30"}</small></label><label>Số từ mục tiêu / chương<input type="number" placeholder="Tự suy luận" min={200} max={5000} step={1} value={targetWords} onChange={e => setTargetWords(e.target.value === "" ? "" : Number(e.target.value))} disabled={saving} /><small>Để trống: suy luận từ ý tưởng · 200–5.000</small></label></div>
    <label className="checkbox-label"><input type="checkbox" checked={autoMode} onChange={e => setAutoMode(e.target.checked)} disabled={saving} /> <span>Chế độ tự động</span><small>Chạy liên tục qua tất cả giai đoạn (theo dõi qua Nhật ký), không tạm dừng chờ duyệt</small></label>
    <p className="form-note">✧ Sau khi tạo, hệ thống tự xây dựng thế giới, nhân vật, dàn ý và viết các chương. Bạn có thể dừng bất cứ lúc nào.</p>
    {error && <div className="error" role="alert">{error}</div>}
    <div className="modal-actions"><button type="button" className="button secondary" onClick={onClose} disabled={saving}>Để sau</button><button className="button primary" disabled={saving || !title.trim() || prompt.trim().length < 10}>{saving ? "Đang tạo…" : "Tạo & bắt đầu viết ↗"}</button></div>
  </form></section></div>;
}