import type { Metadata } from "next";
import { Sidebar } from "@/components/Sidebar";
import "./globals.css";

export const metadata: Metadata = {
  title: "Jim.ai · Fantasy Operations",
  description: "Sleeper fantasy football operations monitor",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body><div className="appShell"><Sidebar/><main className="mainContent">{children}</main></div></body></html>;
}
