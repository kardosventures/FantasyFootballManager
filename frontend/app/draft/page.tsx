import { LiveDraftRoom } from "@/components/LiveDraftRoom";
import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function DraftPage() {
  return <LiveDraftRoom initialDraft={await api.draft()} />;
}
