import Link from "next/link";
import { Icon } from "./Icons";

const items = [
  ["/", "overview", "Operations"],
  ["/draft", "draft", "Draft room"],
  ["/manager", "overview", "Season manager"],
  ["/actions", "actions", "Action queue"],
  ["/schedule", "calendar", "Next 14 days"],
  ["/sources", "sources", "Source health"],
] as const;

export function Sidebar() {
  return <aside className="sidebar">
    <Link href="/" className="brand" aria-label="Fantasy Operations home"><span className="brandMark">JM</span><span><strong>Jim.ai</strong><small>Fantasy operations</small></span></Link>
    <nav aria-label="Primary navigation">{items.map(([href, icon, label]) => <Link key={href} href={href}><Icon name={icon}/><span>{label}</span></Link>)}</nav>
    <div className="sidebarFooter"><span className="statusDot"/><span><strong>Guarded execution</strong><small>Allowlisted writes only</small></span></div>
  </aside>;
}
