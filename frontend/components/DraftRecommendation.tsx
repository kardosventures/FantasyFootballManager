import type { DraftCandidate } from "@/lib/types";

function Candidate({
  candidate,
  primary = false,
}: {
  candidate: DraftCandidate;
  primary?: boolean;
}) {
  const isExpertChoice = candidate.decision_source === "expert_model";
  return (
    <div className={primary ? "candidate primaryCandidate" : "candidate"}>
      <span className="positionBadge">{candidate.position}</span>
      <div>
        <strong>{candidate.player_name}</strong>
        <small>
          {candidate.team ?? "FA"} · ECR {candidate.overall_rank}
          {candidate.position_rank
            ? ` · ${candidate.position}${candidate.position_rank}`
            : ""}
          {candidate.market_adp ? ` · ADP ${candidate.market_adp.toFixed(1)}` : ""}
          {candidate.bye_week ? ` · Bye ${candidate.bye_week}` : ""}
          {candidate.injury_status ? ` · ${candidate.injury_status}` : ""}
        </small>
        {candidate.reasons?.[0] && (
          <span className="candidateReason">{candidate.reasons[0]}</span>
        )}
        {candidate.replacement_player_name && (
          <small>
            Next-turn replacement: {candidate.replacement_player_name}
            {candidate.replacement_draft_value_rank != null
              ? ` · value rank ${candidate.replacement_draft_value_rank.toFixed(1)}`
              : ""}
            {candidate.expected_position_demand_before_next_pick != null
              ? ` · ${candidate.expected_position_demand_before_next_pick.toFixed(1)} ${candidate.position}s expected before turn`
              : ""}
            {candidate.cost_of_waiting != null
              ? ` · wait cost ${candidate.cost_of_waiting.toFixed(2)}`
              : ""}
          </small>
        )}
        {candidate.championship_win_probability != null && (
          <small>
            Modeled title equity: {(
              candidate.championship_win_probability * 100
            ).toFixed(1)}%
            {candidate.championship_equity_delta != null
              ? ` · ${candidate.championship_equity_delta >= 0 ? "+" : ""}${(
                  candidate.championship_equity_delta * 100
                ).toFixed(1)} pts vs candidate median`
              : ""}
            {candidate.championship_simulations
              ? ` · ${candidate.championship_simulations.toLocaleString()} simulations`
              : ""}
          </small>
        )}
      </div>
      <div className="candidateScore">
        <strong>
          {isExpertChoice
            ? `${Math.round(candidate.confidence * 100)}%`
            : candidate.score.toFixed(2)}
        </strong>
        <small>
          {isExpertChoice ? "expert conviction · " : "grounding score · "}
          {candidate.available_at_pick_probability != null
            ? `${Math.round(candidate.available_at_pick_probability * 100)}% at this pick · `
            : ""}
          {Math.round(candidate.survival_probability * 100)}% at next pick
        </small>
      </div>
    </div>
  );
}

export function DraftRecommendation({
  recommendation,
  fallbacks = [],
}: {
  recommendation?: DraftCandidate;
  fallbacks?: DraftCandidate[];
}) {
  if (!recommendation)
    return (
      <div className="emptyState">
        <strong>No recommendation yet</strong>
        <span>
          Generate the draft room after the first Sleeper and rankings sync.
        </span>
      </div>
    );
  return (
    <div className="candidateList">
      <Candidate candidate={recommendation} primary />
      {fallbacks.map((candidate) => (
        <Candidate key={candidate.player_id} candidate={candidate} />
      ))}
    </div>
  );
}
