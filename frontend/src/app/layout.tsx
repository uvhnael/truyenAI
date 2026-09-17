import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "TruyệnAI — Xưởng viết của bạn", description: "Biến ý tưởng thành câu chuyện. Không gian sáng tác truyện tiếng Việt với AI." };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="vi"><body>{children}</body></html>;
}
