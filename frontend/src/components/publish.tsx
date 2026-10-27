"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api, API_URL, errorMessage } from "@/lib/api";

interface SocialConnection {
  connected: boolean;
  display_name: string | null;
}
type SocialStatus = { tiktok: SocialConnection; youtube: SocialConnection };

interface PublishRow {
  id: string;
  video_id: string;
  platform: "tiktok" | "youtube";
  status: string;
  privacy: string;
  caption: string;
  title: string;
  description: string;
  hashtags: string[];
  tags: string[];
  category_id: string;
  schedule_at: string | null;
  remote_id: string | null;
  remote_url: string | null;
  error: string | null;
  generation: number;
  video_generation: number;
  created_at: string;
  updated_at: string;
}

interface Video {
  id: string;
  project_id: string;
  chapter_number: number;
  background: string | null;
  status: string;
  path: string | null;
  duration: number | null;
  width: number | null;
  height: number | null;
  bytes: number | null;
  error: string | null;
  voice: string | null;
  generation: number;
  audio_version: number | null;
  audio_generation: number | null;
  created_at: string;
  updated_at: string;
}

interface VideoItem {
  video: Video;
  publishes: PublishRow[];
}

const VIDEO_LABEL: Record<string, string> = {
  queued: "Trong hàng đợi",
  processing: "Đang tạo…",
  ready: "Sẵn sàng",
  failed: "Lỗi",
  stale: "Hết hạn",
};
const PUBLISH_LABEL: Record<string, string> = {
  queued: "Trong hàng đợi",
  uploading: "Đang tải lên…",
  processing: "Đang xử lý…",
  published: "Đã đăng",
  failed: "Lỗi",
};
const PLATFORM_LABEL: Record<string, string> = { tiktok: "TikTok", youtube: "YouTube" };
const PLATFORM_PLACEHOLDER: Record<"tiktok" | "youtube", string> = { tiktok: "fyp, truyện, vietnam", youtube: "truyen, sachnoi" };

const isPublishBusy = (s: string) => ["queued", "uploading", "processing"].includes(s);

function fileSize(bytes: number | null): string {
  if (!bytes) return "—";
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

export function PublishContent({ defaultProjectId }: { defaultProjectId?: string }) {
  const searchParams = useSearchParams();
  const router = useRouter();
  const projectId = defaultProjectId || searchParams.get("project_id") || "";
  const [social, setSocial] = useState<SocialStatus | null>(null);
  const [videos, setVideos] = useState<VideoItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [banner, setBanner] = useState("");
  const [editing, setEditing] = useState<VideoItem | null>(null);

  // One-time OAuth result banner, then clear the query string.
  useEffect(() => {
    const connected = searchParams.get("connected");
    const oauthError = searchParams.get("error");
    if (connected === "tiktok" || connected === "youtube") {
      setBanner(`Đã kết nối ${PLATFORM_LABEL[connected]}.`);
      router.replace("/publish");
    } else if (oauthError) {
      setBanner("Kết nối không thành công. Vui lòng thử lại.");
      router.replace("/publish");
    }
  }, [searchParams, router]);

  const refreshVideos = useCallback(async (pid?: string) => {
    try {
      const query = pid ? `?project_id=${encodeURIComponent(pid)}` : "";
      const data = await api<VideoItem[]>(`/videos${query}`, undefined);
      setVideos(data);
      setError("");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshSocial = useCallback(async () => {
    try {
      setSocial(await api<SocialStatus>("/social/status"));
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);

  const load = useCallback(async () => {
    await refreshVideos(projectId || undefined);
    await refreshSocial();
  }, [projectId, refreshSocial, refreshVideos]);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll every 5s while any publish (or video) is still being processed.
  const hasBusyItem = useMemo(
    () => videos.some((it) => isPublishBusy(it.video.status) || it.publishes.some((p) => isPublishBusy(p.status))),
    [videos]
  );
  useEffect(() => {
    if (!hasBusyItem) return;
    const timer = setInterval(() => { void refreshVideos(projectId || undefined); }, 5000);
    return () => clearInterval(timer);
  }, [hasBusyItem, projectId, refreshVideos]);

  async function connect(platform: "tiktok" | "youtube") {
    setBusy(`connect-${platform}`);
    try {
      const { url } = await api<{ url: string }>(`/social/${platform}/authorize`);
      window.location.href = url;
    } catch (e) {
      setError(errorMessage(e));
      setBusy(null);
    }
  }

  async function disconnect(platform: "tiktok" | "youtube") {
    setBusy(`disconnect-${platform}`);
    try {
      await api(`/social/${platform}`, { method: "DELETE" });
      await refreshSocial();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  async function retry(publishId: string) {
    setBusy(`retry-${publishId}`);
    try {
      await api(`/publishes/${publishId}/retry`, { method: "POST" });
      await refreshVideos(projectId || undefined);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  if (loading) {
    return <div className="empty-state" role="status">Đang tải trang xuất bản…</div>;
  }

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">ĐƯA TÁC PHẨM ĐẾN VỚI KHÁN GIẢ</div>
          <h1>Xuất bản<span className="accent">.</span></h1>
          <p>Kết nối mạng xã hội và đăng video câu chuyện của bạn.</p>
        </div>
      </div>

      {banner && <div className="demo-banner" role="status"><strong>✧ {banner}</strong></div>}
      {error && <div className="error" role="alert">{error}</div>}

      <section className="connect-grid" aria-label="Kết nối mạng xã hội">
        {(["tiktok", "youtube"] as const).map((platform) => {
          const conn = social?.[platform];
          const connected = !!conn?.connected;
          return (
            <div key={platform} className={`connect-card ${connected ? "connected" : ""}`}>
              <div className="connect-info">
                <div className={`platform-badge ${platform}`} aria-hidden="true">{platform === "tiktok" ? "♫" : "▶"}</div>
                <div>
                  <span className="platform-name">{PLATFORM_LABEL[platform]}</span>
                  <span className={`connect-status ${connected ? "on" : ""}`}>
                    {connected ? (conn?.display_name || `Đã kết nối ${PLATFORM_LABEL[platform]}`) : "Chưa kết nối"}
                  </span>
                </div>
              </div>
              <div className="connect-actions">
                {connected ? (
                  <button className="button danger" onClick={() => void disconnect(platform)} disabled={busy === `disconnect-${platform}`}>{busy === `disconnect-${platform}` ? "Đang ngắt…" : "Ngắt kết nối"}</button>
                ) : (
                  <button className="button primary" onClick={() => void connect(platform)} disabled={busy === `connect-${platform}`}>{busy === `connect-${platform}` ? "Đang mở OAuth…" : `Kết nối ${PLATFORM_LABEL[platform]}`}</button>
                )}
              </div>
            </div>
          );
        })}
      </section>

      <div className="section-heading"><h2>Thư viện video</h2><button className="text-button" onClick={() => { setLoading(true); void load(); }}>↻ Làm mới</button></div>

      <div className="project-grid video-grid">
        {videos.length === 0 ? (
          <div className="empty-state"><span className="empty-icon">▶</span><h3>Chưa có video</h3><p>Tạo video từ chương có audio sẵn sàng trước khi xuất bản.</p></div>
        ) : videos.map((item) => {
          const v = item.video;
          const src = v.status === "ready" ? `${API_URL}/api/projects/${v.project_id}/chapters/${v.chapter_number}/video` : null;
          return (
            <div className="project-card video-card" key={v.id}>
              <div className="video-card-head">
                <h3>Chương {v.chapter_number}</h3>
                <span className={`status ${v.status}`}>{VIDEO_LABEL[v.status] || v.status}</span>
              </div>
              {src
                ? <video controls src={src} className="video-preview" />
                : <div className="video-preview empty-state-stub">{VIDEO_LABEL[v.status] || v.status}</div>}
              <div className="project-card-body">
                <div className="card-meta">
                  <span>{v.duration ? `${Math.round(v.duration)} giây` : "—"}</span>
                  <span>{v.width && v.height ? `${v.width}x${v.height}` : "—"}</span>
                  <span>{fileSize(v.bytes)}</span>
                </div>
                <div className="video-note"><span>Nền: {v.background || "mặc định"}</span></div>
                {v.error && <p className="form-note">{v.error}</p>}

                {item.publishes.length > 0 && (
                  <ul className="publish-list">
                    {item.publishes.map((p) => (
                      <li key={p.id} className={`publish-row ${p.status}`}>
                        <span className={`publish-chip ${p.status}`} title={p.error || ""}>
                          <b>{PLATFORM_LABEL[p.platform]}</b>
                          <i className="publish-dot" />
                          {PUBLISH_LABEL[p.status] || p.status}
                        </span>
                        {p.status === "failed" && <button className="button secondary small" onClick={() => void retry(p.id)} disabled={busy === `retry-${p.id}`}>Thử lại</button>}
                        {p.status === "published" && p.remote_url && <a href={p.remote_url} target="_blank" rel="noreferrer" className="text-button">Xem trên {PLATFORM_LABEL[p.platform]} ↗</a>}
                      </li>
                    ))}
                  </ul>
                )}

                <button className="button primary block" onClick={() => setEditing(item)}>✧ Xuất bản video</button>
              </div>
            </div>
          );
        })}
      </div>

      {editing && <PublishModal key={editing.video.id} item={editing} onClose={() => setEditing(null)} onPublished={() => { setEditing(null); void refreshVideos(projectId || undefined); }} />}
    </>
  );
}

function PublishModal({ item, onClose, onPublished }: { item: VideoItem; onClose: () => void; onPublished: () => void }) {
  const v = item.video;
  const existing = item.publishes[item.publishes.length - 1];
  const initialPlatform: "tiktok" | "youtube" = existing?.platform || "tiktok";
  const [platform, setPlatform] = useState<"tiktok" | "youtube">(initialPlatform);
  const [caption, setCaption] = useState(existing?.caption || "");
  const [hashtags, setHashtags] = useState((existing?.hashtags || []).join(", "));
  const [title, setTitle] = useState(existing?.title || `Truyện chương ${v.chapter_number}`);
  const [description, setDescription] = useState(existing?.description || "");
  const [tags, setTags] = useState((existing?.tags || []).join(", "));
  const [privacy, setPrivacy] = useState(existing?.privacy || (initialPlatform === "tiktok" ? "SELF_ONLY" : "private"));
  const [categoryId, setCategoryId] = useState(existing?.category_id || "22");
  const [scheduleAt, setScheduleAt] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape" && !saving) onClose(); };
    document.addEventListener("keydown", handler);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", handler); document.body.style.overflow = previous; };
  }, [onClose, saving]);

  function splitTags(value: string): string[] {
    return value.split(",").map((t) => t.trim()).filter(Boolean);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError("");
    const payload: Record<string, unknown> = { platform };
    if (platform === "tiktok") {
      payload.caption = caption;
      payload.hashtags = splitTags(hashtags);
      payload.privacy = privacy;
    } else {
      payload.title = title;
      payload.description = description;
      payload.tags = splitTags(tags);
      if (privacy) payload.privacy = privacy;
      if (categoryId) payload.category_id = categoryId;
    }
    if (scheduleAt) payload.schedule_at = scheduleAt;
    try {
      await api(`/videos/${v.id}/publish`, { method: "POST", body: JSON.stringify(payload) });
      onPublished();
    } catch (e) {
      setError(errorMessage(e));
      setSaving(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="publish-title">
        <div className="modal-heading">
          <div><div className="eyebrow">XUẤT BẢN VIDEO</div><h2 id="publish-title">Xuất bản chương {v.chapter_number}</h2></div>
          <button className="icon-button" aria-label="Đóng" disabled={saving} onClick={onClose}>×</button>
        </div>
        <form onSubmit={submit}>
          <div className="form-row">
            <label>Nền tảng<div className="mode-switch" role="radiogroup" aria-label="Nền tảng">
              {(["tiktok", "youtube"] as const).map((p) => (
                <button key={p} type="button" role="radio" aria-checked={platform === p} className={platform === p ? "mode-option selected" : "mode-option"} onClick={() => { setPlatform(p); setPrivacy(p === "tiktok" ? "SELF_ONLY" : "private"); }} disabled={saving}>{PLATFORM_LABEL[p]}</button>
              ))}
            </div></label>
            {platform === "tiktok" ? (
              <label>Chế độ riêng tư<select value={privacy} onChange={(e) => setPrivacy(e.target.value)} disabled={saving}>
                <option value="SELF_ONLY">Chỉ mình tôi</option>
                <option value="PUBLIC_TO_EVERYONE">Công khai</option>
                <option value="MUTUAL_FOLLOW_FRIENDS">Bạn bè</option>
              </select></label>
            ) : (
              <label>Quyền riêng tư<select value={privacy} onChange={(e) => setPrivacy(e.target.value)} disabled={saving}>
                <option value="private">Riêng tư</option>
                <option value="unlisted">Không công khai (unlisted)</option>
                <option value="public">Công khai</option>
              </select></label>
            )}
          </div>

          {platform === "tiktok" ? (
            <>
              <label>Chú thích (caption)<textarea rows={3} value={caption} onChange={(e) => setCaption(e.target.value)} placeholder="Mô tả ngắn cho video TikTok" disabled={saving} /></label>
              <label>Thẻ hashtag<input value={hashtags} onChange={(e) => setHashtags(e.target.value)} placeholder={PLATFORM_PLACEHOLDER.tiktok} disabled={saving} /><small>Phân cách bằng dấu phẩy (,) — không cần dấu #.</small></label>
            </>
          ) : (
            <>
              <label>Tiêu đề<input maxLength={100} value={title} onChange={(e) => setTitle(e.target.value)} disabled={saving} /></label>
              <label>Mô tả<textarea rows={3} value={description} onChange={(e) => setDescription(e.target.value)} disabled={saving} /></label>
              <label>Thẻ (tags)<input value={tags} onChange={(e) => setTags(e.target.value)} placeholder={PLATFORM_PLACEHOLDER.youtube} disabled={saving} /><small>Phân cách bằng dấu phẩy (,).</small></label>
              <label>ID danh mục<input value={categoryId} onChange={(e) => setCategoryId(e.target.value)} placeholder="22" disabled={saving} /></label>
            </>
          )}

          <label>Lên lịch đăng<input type="datetime-local" value={scheduleAt} onChange={(e) => setScheduleAt(e.target.value)} disabled={saving} /><small>Để trống nếu muốn đăng ngay.</small></label>

          {error && <div className="error" role="alert">{error}</div>}
          <div className="modal-actions"><button type="button" className="button secondary" onClick={onClose} disabled={saving}>Để sau</button><button className="button primary" disabled={saving}>{saving ? "Đang đưa vào hàng đợi…" : "Đưa vào hàng đợi ↗"}</button></div>
        </form>
      </section>
    </div>
  );
}