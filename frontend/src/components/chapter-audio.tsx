"use client";

import { useEffect, useId, useRef, useState } from "react";
import { API_URL, api, AudioState, AudioVoices, audioStatusLabel, Chapter, errorMessage } from "@/lib/api";

type PlaybackState = AudioState & { cacheKey: string };
const pending = (status: AudioState["audio_status"]) => status === "queued" || status === "processing";

export default function ChapterAudio({ projectId, chapter, locked, unsaved }: {
  projectId: string; chapter: Chapter; locked: boolean; unsaved: boolean;
}) {
  const voiceId = useId();
  const [voices, setVoices] = useState<AudioVoices | null>(null);
  const [voice, setVoice] = useState(chapter.audio_voice || "");
  const [voiceError, setVoiceError] = useState("");
  const [voiceAttempt, setVoiceAttempt] = useState(0);
  const [state, setState] = useState<PlaybackState>({ audio_status: chapter.audio_status || "none", voice: chapter.audio_voice || null, seconds: chapter.audio_seconds ?? null, error: null, version: null, cacheKey: "" });
  const [checking, setChecking] = useState(true);
  const [statusError, setStatusError] = useState("");
  const [actionError, setActionError] = useState("");
  const [mediaError, setMediaError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const latestState = useRef(state); latestState.current = state;
  const postController = useRef<AbortController | null>(null);
  const path = `/projects/${encodeURIComponent(projectId)}/chapters/${chapter.chapter_number}/audio`;

  useEffect(() => () => postController.current?.abort(), []);
  useEffect(() => {
    const controller = new AbortController();
    setVoiceError("");
    api<AudioVoices>("/audio/voices", { signal: controller.signal }).then(data => {
      if (controller.signal.aborted) return;
      setVoices(data);
      setVoice(previous => data.voices.some(v => v.id === previous) ? previous : data.voices.some(v => v.id === data.default_voice) ? data.default_voice : data.voices[0]?.id || "");
      if (!data.voices.length) setVoiceError("Máy chủ chưa có giọng đọc khả dụng.");
    }).catch(error => { if (!controller.signal.aborted) setVoiceError(errorMessage(error)); });
    return () => controller.abort();
  }, [voiceAttempt]);

  useEffect(() => {
    if (submitting) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setChecking(true);
    async function check() {
      let nextStatus = latestState.current.audio_status;
      try {
        const data = await api<AudioState>(`${path}/status`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        nextStatus = data.audio_status;
        setState(previous => ({ ...data, cacheKey: data.audio_status === "ready" && (previous.audio_status !== "ready" || previous.version !== data.version || previous.voice !== data.voice || !previous.cacheKey) ? String(Date.now()) : previous.cacheKey }));
        setStatusError("");
      } catch (error) {
        if (controller.signal.aborted) return;
        setStatusError(errorMessage(error));
      } finally {
        if (!controller.signal.aborted) {
          setChecking(false);
          // No idle/ready/failed polling; retry transient errors only for active jobs.
          if (pending(nextStatus)) timer = setTimeout(() => void check(), 3000);
        }
      }
    }
    void check();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [path, chapter.version, chapter.audio_status, chapter.audio_voice, submitting, refresh]);

  const working = submitting || pending(state.audio_status);
  const disabled = locked || unsaved || !chapter.content.trim() || working || checking || !!statusError || !voices?.voices.some(v => v.id === voice);
  async function generate() {
    if (disabled || postController.current) return;
    const controller = new AbortController(); postController.current = controller;
    setSubmitting(true); setActionError(""); setMediaError("");
    try {
      await api<unknown>(`${path}?voice=${encodeURIComponent(voice)}`, { method: "POST", signal: controller.signal });
      if (!controller.signal.aborted) setState(previous => ({ ...previous, audio_status: "queued", voice, error: null, seconds: null }));
    } catch (error) { if (!controller.signal.aborted) setActionError(errorMessage(error)); }
    finally { postController.current = null; if (!controller.signal.aborted) setSubmitting(false); }
  }
  const audioUrl = `${API_URL}/api${path}?${new URLSearchParams({ version: String(state.version ?? chapter.version), voice: state.voice || "", status: state.audio_status, t: state.cacheKey })}`;
  const duration = state.seconds != null ? `${Math.floor(state.seconds / 60)}:${String(Math.floor(state.seconds % 60)).padStart(2, "0")}` : "";
  const ready = state.audio_status === "ready" && !!state.cacheKey;

  return <section className="chapter-audio" aria-label={`Audio chương ${chapter.chapter_number}`}>
    <div className="audio-heading"><div><strong>Nghe chương truyện</strong><small>Kokoro · Giọng nói tổng hợp bằng AI</small></div><span className="tag subtle" role="status">{submitting ? "Đang gửi yêu cầu…" : audioStatusLabel(state.audio_status)}{duration && ` · ${duration}`}</span></div>
    <div className="audio-actions"><label htmlFor={voiceId}>Giọng đọc<select id={voiceId} value={voice} disabled={working || !voices?.voices.length} onChange={event => setVoice(event.target.value)}>{!voices?.voices.length && <option value="">Đang tải giọng đọc…</option>}{voices?.voices.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><button className="button secondary" disabled={disabled} onClick={() => void generate()}>{working ? "Đang tạo audio…" : state.audio_status === "failed" || actionError ? "Thử tạo lại audio" : state.audio_status === "none" ? "Tạo audio" : "Tạo lại audio"}</button></div>
    {locked ? <p className="muted">Chỉ tạo audio sau khi tác vụ viết truyện hoàn thành.</p> : unsaved ? <p className="muted">Lưu hoặc bỏ thay đổi trong trình sửa trước khi tạo audio.</p> : null}
    {state.audio_status === "stale" && <p className="warning-banner">Nội dung chương đã thay đổi. Tạo lại audio để nghe bản mới nhất.</p>}
    {pending(state.audio_status) && <p className="muted">Đang tổng hợp giọng nói, có thể mất vài phút. Trạng thái tự cập nhật mỗi 3 giây.</p>}
    {voiceError && <div className="error" role="alert">{voiceError} <button className="text-button" onClick={() => setVoiceAttempt(n => n + 1)}>Tải lại giọng đọc</button></div>}
    {statusError && <div className="error" role="alert">{statusError} <button className="text-button" onClick={() => setRefresh(n => n + 1)}>Kiểm tra lại trạng thái</button></div>}
    {(actionError || state.error || state.audio_status === "failed") && <p className="error" role="alert">{actionError || state.error || "Không thể tạo audio. Vui lòng thử lại."}</p>}
    {ready && <div className="audio-playback"><audio key={audioUrl} controls preload="none" src={audioUrl} aria-label={`Nghe chương ${chapter.chapter_number}`} onError={() => setMediaError("Không thể phát audio. Kiểm tra kết nối hoặc tạo lại audio.")} onLoadedMetadata={() => setMediaError("")} /><a className="text-button" href={audioUrl} download={`chuong-${chapter.chapter_number}.mp3`} target="_blank" rel="noreferrer">↓ Tải MP3</a><small className="muted">Giọng bản audio: {voices?.voices.find(item => item.id === state.voice)?.name || state.voice || "—"}</small></div>}
    {mediaError && ready && <p className="error" role="alert">{mediaError}</p>}
  </section>;
}
