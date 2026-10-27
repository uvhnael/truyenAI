"use client";

import Link from "next/link";
import { ReactNode, useEffect, useState } from "react";
import { api, isBusy, Project } from "@/lib/api";

interface AppShellProps {
  /** Breadcrumb text shown in the top bar, e.g. "Thư viện", "Xuất bản", "Bản thảo". */
  crumb: string;
  /** Which sidebar item is highlighted. */
  active?: "library" | "publish";
  /** When provided, renders the "Ý tưởng mới" button wired to a create modal. */
  onCreateNew?: () => void;
  children: ReactNode;
}

/**
 * Shared application shell (sidebar navigation + top bar + mode banner + footer).
 * Keeps navigation consistent across every page so users can always get back to
 * their library or open the create flow.
 */
export default function AppShell({ crumb, active, onCreateNew, children }: AppShellProps) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [mode, setMode] = useState<"mock" | "live" | "unknown">("unknown");

  useEffect(() => {
    // Silently resolve health; sidebar library count is best-effort.
    let alive = true;
    const refresh = () =>
      api<{ llm_mode: "mock" | "live" }>("/health")
        .then((d) => { if (alive) setMode(d.llm_mode); })
        .catch(() => { if (alive) setMode("unknown"); });
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 15000);
    const loadProjects = () =>
      api<Project[]>("/projects")
        .then((list) => { if (alive) setProjects(list); })
        .catch(() => { /* keep stale list */ });
    void loadProjects();
    const pTimer = setInterval(() => { void loadProjects(); }, 15000);
    return () => { alive = false; clearInterval(timer); clearInterval(pTimer); };
  }, []);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Link className="brand" href="/"><span className="brand-icon">✦</span><span>Truyện<span className="brand-accent">AI</span><small>KHÔNG GIAN SÁNG TÁC</small></span></Link>
        <div className="sidebar-section">XƯỞNG VIẾT</div>
        <Link href="/" className={`nav-item ${active === "library" ? "selected" : ""}`}><span>▤</span> Thư viện truyện <span className="nav-count">{projects.length}</span></Link>
        {onCreateNew && <button className="nav-item" onClick={onCreateNew}><span>＋</span> Ý tưởng mới</button>}
        <Link href="/publish" className={`nav-item ${active === "publish" ? "selected" : ""}`}><span>↗</span> Xuất bản</Link>
        <div className="sidebar-section recent-heading">GẦN ĐÂY</div>
        <nav className="recent-projects" aria-label="Dự án gần đây">{projects.slice(0, 7).map(p => <Link key={p.id} href={`/projects/${p.id}`} className="recent-item"><span className={`dot ${isBusy(p.status) ? "pulse" : ""}`} />{p.title}</Link>)}</nav>
        <div className="sidebar-note"><span>✧</span><strong>Câu chuyện bắt đầu từ bạn.</strong><p>AI giúp dựng thế giới.<br />Bạn giữ linh hồn tác phẩm.</p></div>
        <div className="sidebar-footer"><span className="avatar">T</span><div>Xưởng viết cá nhân<small>TRUYỆNAI · MVP</small></div></div>
      </aside>
      <div className="workspace">
        <header className="topbar"><span>Không gian của bạn <span className="crumb">/ {crumb}</span></span><span className={`mode-pill ${mode}`}><span className="dot" />{mode === "live" ? "Gemini · Trực tiếp" : mode === "mock" ? "Chế độ demo" : "Đang kiểm tra API"}</span></header>
        {mode === "mock" && <div className="demo-banner" role="status"><strong>✧ Đang dùng dữ liệu mô phỏng.</strong> Chưa gọi Gemini hay tiêu tốn token. Thêm API key ở máy chủ và chuyển sang chế độ live để tạo truyện bằng AI.</div>}
        {mode === "unknown" && <div className="connection-banner">Chưa xác định chế độ AI. Kiểm tra kết nối máy chủ trước khi tạo truyện.</div>}
        <main id="main-content">{children}</main>
        <footer className="page-footer">Viết bằng trí tưởng tượng. Đồng hành bởi AI.<span>TruyệnAI / 2026</span></footer>
      </div>
    </div>
  );
}