import { Suspense } from "react";
import AppShell from "@/components/app-shell";
import { PublishContent } from "@/components/publish";

export default function PublishPage() {
  return (
    <AppShell crumb="Xuất bản" active="publish">
      <Suspense fallback={<div className="empty-state" role="status">Đang tải trang xuất bản…</div>}>
        <PublishContent />
      </Suspense>
    </AppShell>
  );
}