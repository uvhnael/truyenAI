"use client";

import { useEffect, useRef, useState } from "react";
import { API_URL, api, Chapter, errorMessage } from "@/lib/api";

type VideoStatus = "none" | "queued" | "processing" | "ready" | "failed" | "stale";
interface VideoState {
  video_status: VideoStatus;
  background: string | null;
  duration: number | null;
  width: number | null;
  height: number | null;
  bytes: number | null;
  error: string | null;
  generation: number;
}
interface VideoBackgrounds { backgrounds: string[]; default: string | null }

const videoStatusLabel = (s?: string) => ({ none: "Chưa có video", queued: "Đang chờ tạo…", processing: "Đang tạo…", ready: "Sẵn sàng", failed: "Lỗi", stale: "Cần tạo lại" }[s || ""] || s);
const pending = (s: VideoStatus) => s === "queued" || s === "processing";
const fileSize = (bytes: number | null) => {
  if (!bytes) return "";
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : bytes >= 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${bytes} B`;
};

export default function ChapterVideo({ projectId, chapter, locked, unsaved }: {
  projectId: string; chapter: Chapter; locked: boolean; unsaved: boolean;
}) {
  const [bgItems, setBgItems] = useState<string[]>([]);
  const [background, setBackground] = useState("");
  const [bgError, setBgError] = useState("");
  const [bgAttempt, setBgAttempt] = useState(0);
  const [state, setState] = useState<VideoState>({ video_status: "none", background: null, duration: null, width: null, height: null, bytes: null, error: null, generation: 0 });
  const [actionError, setActionError] = useState("");
  const [statusError, setStatusError] = useState("");
  const [checking, setChecking] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const latestState = useRef(state); latestState.current = state;
  const path = `/projects/${encodeURIComponent(projectId)}/chapters/${chapter.chapter_number}/video`;

  // Nền video
  useEffect(() => {
    const controller = new AbortController();
    setBgError("");
    api<VideoBackgrounds>("/video/backgrounds", { signal: controller.signal }).then(data => {
      if (controller.signal.aborted) return;
      setBgItems(data.backgrounds);
      setBackground(prev => data.backgrounds.includes(prev) ? prev : data.backgrounds[0] || "");
      if (!data.backgrounds.length) setBgError("Máy chủ chưa có video nền. Thêm file .mp4 vào thư mục data/video-backgrounds rồi khởi động lại stack.");
    }).catch(error => { if (!controller.signal.aborted) setBgError(errorMessage(error)); });
    return () => controller.abort();
  }, [bgAttempt]);

  // Trạng thái video khi chương có audio sẵn sàng
  const audioReady = chapter.audio_status === "ready";
  useEffect(() => {
    if (submitting || !audioReady) { setChecking(audioReady); return; }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setChecking(true);
    async function check() {
      let next = latestState.current.video_status;
      try {
        const data = await api<VideoState>(`${path}/status`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        next = data.video_status;
        setState(data);
        if (data.video_status !== "none" && data.background && !background) setBackground(data.background);
        setStatusError("");
      } catch (error) {
        if (controller.signal.aborted) return;
        setStatusError(errorMessage(error));
      } finally {
        if (!controller.signal.aborted) {
          setChecking(false);
          if (pending(next)) timer = setTimeout(() => void check(), 3000);
        }
      }
    }
    void check();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, audioReady, chapter.version, submitting, refresh, background]);

  const working = submitting || pending(state.video_status);
  const noBgs = bgItems.length === 0;
  const disabled = locked || unsaved || !audioReady || noBgs || working || checking;
  async function create() {
    if (disabled || !audioReady) return;
    setSubmitting(true); setActionError(""); setStatusError("");
    try {
      const body = background ? { background } : {};
      await api<{ status: string; video_id: string }>(path, { method: "POST", body: JSON.stringify(body) });
      setState(prev => ({ ...prev, video_status: "queued", error: null }));
      setRefresh(n => n + 1);
    } catch (error) {
      setActionError(errorMessage(error));
    } finally { setSubmitting(false); }
  }

  const ready = state.video_status === "ready";
  const videoUrl = ready ? `${API_URL}/api${path}?t=${state.generation}` : null;
  const duration = state.duration != null ? `${Math.floor(state.duration / 60)}:${String(Math.floor(state.duration % 60)).padStart(2, "0")}` : "";

  return <section className="chapter-audio" aria-label={`Video chương ${chapter.chapter_number}`}>
    <div className="audio-heading"><div><strong>Tạo video chương</strong><small>Video nền · kèm lời đọc</small></div><span className="tag subtle" role="status">{submitting ? "Đang gửi yêu cầu…" : videoStatusLabel(state.video_status)}{duration && ` · ${duration}`}</span></div>
    <div className="audio-actions">
      <label>Nền<select value={background} disabled={working || noBgs} onChange={e => setBackground(e.target.value)}>
        {noBgs ? <option value="">Chưa có video nền…</option> : bgItems.map(b => <option key={b} value={b}>{b}</option>)}
      </select></label>
      <button className="button secondary" disabled={disabled} onClick={() => void create()}>
        {working ? "Đang tạo video…" : state.video_status === "failed" || actionError ? "Thử tạo lại video" : state.video_status === "ready" ? "Tạo lại video" : "Tạo video"}
      </button>
    </div>
    {locked ? <p className="muted">Chỉ tạo video sau khi tác vụ viết truyện hoàn thành.</p>
      : unsaved ? <p className="muted">Lưu hoặc bỏ thay đổi trong trình sửa trước khi tạo video.</p>
      : !audioReady ? <p className="muted">Tạo audio cho chương này trước (video ghép lời đọc + video nền).</p>
      : null}
    {bgError && <div className="error" role="alert">{bgError} <button className="text-button" onClick={() => setBgAttempt(n => n + 1)}>Kiểm tra lại</button></div>}
    {state.video_status === "stale" && <p className="warning-banner">Audio đã đổi so với bản video này. Tạo lại video để cập nhật.</p>}
    {pending(state.video_status) && <p className="muted">Đang render video, có thể mất vài phút. Trạng thái tự cập nhật mỗi 3 giây.</p>}
    {statusError && <div className="error" role="alert">{statusError} <button className="text-button" onClick={() => setRefresh(n => n + 1)}>Kiểm tra lại trạng thái</button></div>}
    {(actionError || state.error || state.video_status === "failed") && <p className="error" role="alert">{actionError || state.error || "Không thể tạo video. Vui lòng thử lại."}</p>}
    {ready && videoUrl && <div className="audio-playback">
      <video controls preload="none" src={videoUrl} className="video-preview" aria-label={`Video chương ${chapter.chapter_number}`} />
      <a className="text-button" href={videoUrl} download target="_blank" rel="noreferrer">↓ Tải MP4</a>
      <small className="muted">Nền: {state.background || "mặc định"}{state.width && state.height ? ` · ${state.width}x${state.height}` : ""}{state.bytes ? ` · ${fileSize(state.bytes)}` : ""}</small>
    </div>}
  </section>;
}