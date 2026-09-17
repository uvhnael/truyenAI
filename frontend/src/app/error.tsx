"use client";
export default function ErrorPage({ reset }: { reset: () => void }) { return <main><div className="empty-state"><h1>Không thể hiển thị trang</h1><p>Dữ liệu có thể vừa thay đổi. Hãy tải lại để tiếp tục.</p><button className="button primary" onClick={reset}>Thử lại</button> <a href="/" className="button secondary">Về thư viện</a></div></main>; }
