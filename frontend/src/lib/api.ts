export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:18080").replace(/\/$/, "");
export interface Chapter { id: string; chapter_number: number; title: string; content: string; summary: string; status: string; word_count: number; version: number }
export interface Revision { id: string; revision_no: number; content: string; review: unknown; source: string; created_at: string }
export interface StoryEvent { id: string | number; type: string; payload: Record<string, unknown> }
export interface Project { id: string; title: string; prompt: string; status: string; options: { chapter_count?: number | null; target_words?: number | null }; spec?: { chapter_count?: number; target_words?: number }; created_at: string; bible?: Record<string, unknown> | null; characters?: { name: string; role: string; description: string; arc: unknown }[]; outline?: { chapter_number: number; title: string; summary: string; beats: unknown[] }[]; chapters?: Chapter[]; job?: { id: string; status: string; stage: string; error?: string | null } | null; events?: StoryEvent[] }
export class ApiError extends Error { constructor(message: string, public status: number) { super(message); } }
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try { response = await fetch(`${API_URL}/api${path}`, { ...init, headers: { "Content-Type": "application/json", ...init?.headers }, cache: "no-store" }); }
  catch (error) { if (error instanceof Error && error.name === "AbortError") throw error; throw new Error("Không thể kết nối máy chủ. Kiểm tra API và thử lại."); }
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const detail = data?.detail;
    const message = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((e: { msg: string }) => e.msg).join("; ") : `Yêu cầu thất bại (${response.status}).`;
    throw new ApiError(message, response.status);
  }
  return response.json();
}
export const isBusy = (status?: string) => ["queued", "running", "pending", "cancelling"].includes(status || "");
export const isDead = (status?: string) => ["failed", "cancelled"].includes(status || "");
export const statusLabel = (status: string) => ({ draft: "Bản nháp", pending: "Chờ xử lý", queued: "Trong hàng đợi", running: "Đang sáng tác", completed: "Hoàn thành", complete: "Hoàn thành", done: "Hoàn thành", final: "Bản hoàn chỉnh", needs_review: "Cần duyệt lại", edited: "Đã chỉnh sửa", failed: "Có lỗi", cancelled: "Đã dừng", cancelling: "Đang dừng" }[status] || status);
export function canEdit(project: Project) { return ["completed", "complete", "done", "needs_review", "edited"].includes(project.status) && !isBusy(project.job?.status) && !isDead(project.job?.status) && (!project.job || ["done", "completed", "complete"].includes(project.job.status)); }
export function editLockReason(project: Project) { return isBusy(project.status) || isBusy(project.job?.status) ? "AI đang viết. Trình sửa tạm khóa để tránh ghi đè; bạn vẫn có thể đọc bản thảo và lịch sử." : "Dự án đang dừng hoặc gặp lỗi. Chạy lại và đợi hoàn thành để tiếp tục chỉnh sửa."; }
export function dateLabel(date: string) { return new Date(date).toLocaleString("vi-VN", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }); }
export function errorMessage(error: unknown) { return error instanceof Error ? error.message : "Đã có lỗi xảy ra. Vui lòng thử lại."; }
