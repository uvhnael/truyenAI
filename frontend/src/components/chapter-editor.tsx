"use client";
import { useEffect, useRef, useState } from "react";
import { api, ApiError, Chapter, dateLabel, errorMessage, Revision, statusLabel } from "@/lib/api";
import ChapterAudio from "./chapter-audio";
import ChapterVideo from "./chapter-video";

export default function ChapterEditor({ projectId, chapters, busy, lockReason, onDirtyChange, onSaved }: { projectId: string; chapters: Chapter[]; busy: boolean; lockReason: string; onDirtyChange: (value: boolean) => void; onSaved: () => Promise<void> }) {
  const [selected, setSelected] = useState(chapters[0]?.chapter_number || 1);
  const [draft, setDraft] = useState<Chapter | null>(null);
  const [dirty, setDirty] = useState(false);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(""); const [notice, setNotice] = useState("");
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [revisionError, setRevisionError] = useState(""); const [revisionLoading, setRevisionLoading] = useState(false);
  const [showRevisions, setShowRevisions] = useState(false); const [revision, setRevision] = useState<Revision | null>(null);
  const selection = useRef(selected); selection.current = selected;
  const current = chapters.find(c => c.chapter_number === selected) || chapters[0];
  const currentId = current?.id; const currentVersion = current?.version; const currentContent = current?.content; const currentTitle = current?.title;
  useEffect(() => {
    if (!dirty && current) setDraft({ ...current });
    // Server polling must never overwrite an unsaved local draft.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentId, currentVersion, currentContent, currentTitle, dirty]);
  useEffect(() => { if (!dirty) return; const handler = (e: BeforeUnloadEvent) => { e.preventDefault(); }; window.addEventListener("beforeunload", handler); const clickHandler = (e: MouseEvent) => { if ((e.target as HTMLElement).closest("a[href]") && !window.confirm("Bạn có thay đổi chưa lưu. Rời trang và bỏ thay đổi?")) { e.preventDefault(); e.stopPropagation(); } }; document.addEventListener("click", clickHandler, true); return () => { window.removeEventListener("beforeunload", handler); document.removeEventListener("click", clickHandler, true); }; }, [dirty]);
  useEffect(() => {
    if (!showRevisions) return;
    const controller = new AbortController(); setRevisionLoading(true); setRevisionError("");
    api<Revision[]>(`/projects/${encodeURIComponent(projectId)}/chapters/${selected}/revisions`, { signal: controller.signal }).then(data => { setRevisions([...data].sort((a, b) => b.revision_no - a.revision_no)); }).catch(e => { if (!controller.signal.aborted) setRevisionError(errorMessage(e)); }).finally(() => { if (!controller.signal.aborted) setRevisionLoading(false); });
    return () => controller.abort();
  }, [projectId, selected, showRevisions, currentVersion]);
  function choose(n: number) { if (n === selected || saving) return; if (dirty && !window.confirm("Bỏ thay đổi chưa lưu để mở chương khác?")) return; setDirty(false); setDraft(chapters.find(c => c.chapter_number === n) || null); setSelected(n); setRevision(null); setRevisions([]); setNotice(""); setError(""); }
  async function save() {
    if (!draft || busy || saving) return;
    const chapterNumber = selected; setSaving(true); setError(""); setNotice("");
    try { const saved = await api<Chapter>(`/projects/${encodeURIComponent(projectId)}/chapters/${chapterNumber}`, { method: "PUT", body: JSON.stringify({ title: draft.title.trim(), content: draft.content, version: draft.version }) }); if (selection.current === chapterNumber) { setDraft(saved); setDirty(false); setNotice("Đã lưu bản chỉnh sửa."); } await onSaved(); }
    catch (e) { setError(e instanceof ApiError && e.status === 409 ? "Bản thảo trên máy chủ đã thay đổi hoặc AI đang viết. Nội dung của bạn vẫn được giữ lại. Sao chép nội dung trước khi tải bản mới nhất." : errorMessage(e)); }
    finally { setSaving(false); }
  }
  async function reload() { if (dirty && !window.confirm("Tải bản trên máy chủ sẽ bỏ thay đổi chưa lưu. Tiếp tục?")) return; setError(""); try { const data = await api<Chapter>(`/projects/${encodeURIComponent(projectId)}/chapters/${selected}`); setDraft(data); setDirty(false); setNotice("Đã tải bản mới nhất."); await onSaved(); } catch (e) { setError(errorMessage(e)); } }
  const words = draft?.content.trim() ? draft.content.trim().split(/\s+/u).length : 0;
  return <div className="chapter-layout"><aside className="chapter-list" aria-label="Danh sách chương"><div className="chapter-list-heading">BẢN THẢO <span>{chapters.length}</span></div>{chapters.map(c => <button key={c.chapter_number} className={selected === c.chapter_number ? "active" : ""} onClick={() => choose(c.chapter_number)} disabled={saving}><span className="chapter-number">{String(c.chapter_number).padStart(2, "0")}</span><span><strong>{c.title || `Chương ${c.chapter_number}`}</strong><small>{c.word_count || 0} từ · {statusLabel(c.status)}</small></span></button>)}</aside>
    <div className="editor-card">
      <div className="editor-toolbar"><span className="muted">Chương {selected} · Phiên bản {draft?.version ?? "—"}{dirty && <span className="unsaved"> · Chưa lưu</span>}</span><button className={`text-button ${showRevisions ? "accent" : ""}`} onClick={() => { setShowRevisions(!showRevisions); setRevision(null); }}>◷ {showRevisions ? "Đóng lịch sử" : "Lịch sử phiên bản"}</button></div>
      {current && <ChapterAudio key={`${projectId}-${current.id}`} projectId={projectId} chapter={current} locked={busy} unsaved={dirty || saving} />}
      {current && <ChapterVideo key={`${projectId}-${current.id}-video`} projectId={projectId} chapter={current} locked={busy} unsaved={dirty || saving} />}
      {busy && <div className="info-banner">{lockReason}</div>}
      {dirty && current && current.version !== draft?.version && <div className="warning-banner">Máy chủ có phiên bản mới hơn. Bản nháp chưa lưu của bạn vẫn được giữ nguyên.</div>}
      {error && <div className="error" role="alert">{error}<button className="text-button" disabled={saving} onClick={() => void reload()}>Tải bản mới nhất</button></div>}
      {notice && <div className="success" role="status">{notice}</div>}
      {showRevisions && <section className="revision-panel" aria-label="Lịch sử chỉnh sửa"><h3>Lịch sử phiên bản</h3>{revisionLoading ? <p role="status">Đang tải lịch sử…</p> : revisionError ? <p className="error" role="alert">{revisionError}</p> : revisions.length ? <><label>Chọn bản để xem<select value={revision?.id || ""} onChange={e => setRevision(revisions.find(r => r.id === e.target.value) || null)}><option value="">— Chọn phiên bản —</option>{revisions.map(r => <option key={r.id} value={r.id}>Bản {r.revision_no} · {r.source === "manual" || r.source === "user" ? "Chỉnh sửa thủ công" : r.source} · {dateLabel(r.created_at)}</option>)}</select></label>{revision && <div className="revision-content"><div className="section-heading"><strong>Bản {revision.revision_no}</strong><button className="text-button" disabled={busy || saving} onClick={() => { if (dirty && !window.confirm("Thay bản nháp chưa lưu bằng phiên bản này?")) return; if (draft) { setDraft({ ...draft, content: revision.content }); setDirty(true); setNotice("Đã đưa phiên bản vào trình sửa. Nhấn Lưu để tạo bản chỉnh sửa mới."); setRevision(null); setShowRevisions(false); } }}>Dùng nội dung bản này</button></div><div className="revision-prose">{revision.content}</div>{revision.review != null && <details><summary>Nhận xét biên tập</summary><pre>{typeof revision.review === "string" ? revision.review : JSON.stringify(revision.review, null, 2)}</pre></details>}</div>}</> : <p className="muted">Chưa có phiên bản lưu trữ.</p>}</section>}
      {draft ? <><label className="editor-title-label"><span className="sr-only">Tiêu đề chương</span><input className="editor-title" value={draft.title} maxLength={300} disabled={busy || saving} onChange={e => { setDraft({ ...draft, title: e.target.value }); setDirty(true); setNotice(""); }} /></label>{draft.summary && <details className="chapter-summary"><summary>Tóm tắt chương</summary><p>{draft.summary}</p></details>}<label className="editor-body-label"><span className="sr-only">Nội dung chương</span><textarea className="chapter-textarea" value={draft.content} readOnly={busy || saving} onChange={e => { setDraft({ ...draft, content: e.target.value }); setDirty(true); setNotice(""); }} placeholder="Nội dung chương truyện…" spellCheck={false} /></label><div className="editor-bottom"><span>{words.toLocaleString("vi-VN")} từ <span className="muted">· Không tự động lưu</span></span><div className="action-group"><button className="button secondary" disabled={saving || !dirty} onClick={() => void reload()}>Bỏ thay đổi</button><button className="button primary" disabled={busy || saving || !dirty || !draft.title.trim() || !draft.content.trim()} onClick={() => void save()}>{saving ? "Đang lưu…" : "Lưu chỉnh sửa"}</button></div></div></> : <p>Đang tải chương…</p>}
    </div>
  </div>;
}
