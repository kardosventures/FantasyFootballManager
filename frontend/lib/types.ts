export type Check = {
  id: string;
  status: "pass" | "block" | "warning" | "not_applicable";
  summary: string;
  evidence?: unknown;
};

export type Readiness = {
  generated_at?: string;
  ready: boolean;
  blocker_count?: number;
  league?: { name?: string; season?: string; status?: string };
  checks: Check[];
  discrepancies: Array<{ field: string; severity: string; message: string }>;
  capabilities?: {
    platform_ready?: boolean;
    data_ready?: boolean;
    manager_decision_ready?: boolean;
    autopilot_ready?: boolean;
    autopilot_state?: "ready" | "blocked";
    autopilot_blockers?: string[];
    execution_domains?: Record<string, string>;
    lineup_execution_ready?: boolean;
    waiver_decision_ready?: boolean;
    waiver_selector_qualified?: boolean;
    waiver_submission_qualified?: boolean;
    waiver_execution_ready?: boolean;
    waiver_fallback_qualified?: boolean;
    waiver_fallback_execution_ready?: boolean;
    pending_waiver_decision_ready?: boolean;
    pending_waiver_submission_qualified?: boolean;
    pending_waiver_qualified_actions?: string[];
    pending_waiver_execution_ready?: boolean;
    free_agent_decision_ready?: boolean;
    free_agent_submission_qualified?: boolean;
    free_agent_execution_ready?: boolean;
    sequential_free_agent_execution_ready?: boolean;
    ir_decision_ready?: boolean;
    ir_applicability?: "available" | "not_applicable";
    ir_submission_qualified?: boolean;
    ir_qualified_actions?: string[];
    ir_execution_ready?: boolean;
    trade_intelligence_ready?: boolean;
    trade_submission_qualified?: boolean;
    trade_qualified_actions?: string[];
    trade_execution_ready?: boolean;
    transaction_guard_mode?: "qualified_only" | "trust_then_verify";
    acquisition_reconciliation_ready?: boolean;
    dashboard_notifications_ready?: boolean;
    slack_notifications_ready?: boolean;
    intelligence_authority?: string;
  };
};

export type ActionItem = {
  id: string;
  action_type: string;
  title: string;
  status: string;
  priority: number;
  exact_action: Record<string, unknown>;
  primary_reason: string;
  confidence: number;
  approval_required: boolean;
  drop_protection_tier?: string;
  decision_policy_version: string;
  created_at: string;
  can_cancel: boolean;
  execution?: {
    id: string;
    status: string;
    not_before: string;
    expires_at: string;
    attempt_count: number;
    result?: Record<string, unknown> | null;
  } | null;
};

export type ManagerPlayer = {
  player_id: string;
  name: string;
  sleeper_ui_display_name?: string;
  position: string;
  team?: string;
  injury_status?: string;
  practice_status?: string;
  locked?: boolean;
  overall_rank?: number;
  projection_ensemble?: {
    status: string;
    mean?: number;
    disagreement?: number;
    confidence?: number;
    weight_profile?: string;
    calibration_status?: string;
    sources?: Array<{ source: string; points: number; weight: number }>;
  };
};

export type FantasyProsQuota = {
  configured: boolean;
  status: string;
  daily_limit?: number;
  reserved_requests?: number;
  used?: number;
  standard_remaining?: number;
  total_remaining?: number;
  last_refresh_at?: string | null;
  target_player_count?: number;
  target_player_coverage?: number;
  last_refresh_requests?: number;
  cooldown_remaining_seconds?: number;
  emergency_refresh_available?: boolean;
  refresh_status?: string;
  requests_this_refresh?: number;
};

export type TrashTalkStatus = {
  configured: boolean;
  enabled: boolean;
  paused_reason?: string | null;
  opted_out_roster_ids: number[];
  weekly_target: number;
  weekly_limit: number;
  weekly_count: number;
  image_upload_configured: boolean;
  posts: Array<{
    id: string;
    trigger_type: string;
    target_label?: string | null;
    message?: string | null;
    format: string;
    status: string;
    quality_score: number;
    safety: Record<string, unknown>;
    suppression_reason?: string | null;
    evidence: Record<string, unknown>;
    created_at: string;
    delivered_at?: string | null;
  }>;
};

export type ProjectionBacktest = {
  generated_at?: string;
  status: string;
  qualification_status: string;
  ensemble_calibration?: {
    qualification_status: string;
    completed_weeks: string[];
    candidate: {
      samples?: number;
      mae?: number | null;
      rmse?: number | null;
      p20_p80_coverage?: number | null;
    };
    strongest_paired_source?: string | null;
    blockers: string[];
    checks: Array<{
      id: string;
      status: string;
      observed?: unknown;
      required?: unknown;
    }>;
    snapshot_accounting?: {
      persisted?: number;
      eligible_pregame_player_weeks?: number;
    };
  };
};

export type ChampionshipBacktest = {
  generated_at?: string;
  status: string;
  qualification_status: string;
  qualification_blockers?: string[];
  feature_qualification?: Record<string, {
    qualification_status: string;
    evidence?: string;
  }>;
  component_calibration?: Record<string, {
    qualification_status: string;
    samples?: number;
    [key: string]: unknown;
  }>;
  season_probability_calibration?: {
    qualification_status: string;
    weekly_win_probability?: {
      samples: number;
      brier_score?: number | null;
      log_loss?: number | null;
      calibration_error?: number | null;
    };
  };
};

export type ManagerPlan = {
  generated_at?: string;
  manager_state: string;
  autopilot_state?: "ready" | "blocked";
  autopilot_ready?: boolean;
  autopilot_blockers?: string[];
  execution_domains?: Record<string, string>;
  execution_state: string;
  week?: number;
  blockers: string[];
  season_horizon?: {
    objective: string;
    current_week: number;
    regular_season_end_week: number;
    playoff_weeks: number[];
    next_waiver_processing_at: string;
    pending_waiver_claims?: Array<{
      transaction_id: string;
      created?: number;
      add_player_ids: string[];
      add_player_names: string[];
      drop_player_ids: string[];
      drop_player_names: string[];
      sequence?: number;
      waiver_bid?: number;
    }>;
    evaluation_windows: {
      next_four_weeks: number[];
      fantasy_playoffs: number[];
    };
    roster_construction: {
      starter_slots: string[];
      position_counts: Record<string, number>;
      active_roster_count: number;
      roster_capacity: number;
      open_roster_slots: number;
      bench_player_ids: string[];
      bye_week_exposure: Record<string, Array<{ player_id: string; name: string; position: string }>>;
    };
    rental_policy: string;
  };
  intelligence?: {
    mode: "shadow" | "authoritative";
    usage_features_enabled: boolean;
    projection_ensemble_enabled: boolean;
    championship_simulation_enabled: boolean;
    snapshots_persisted: {
      usage: number;
      projections: number;
      simulations: number;
    };
    snapshots_added: {
      usage: number;
      projections: number;
      simulations: number;
    };
    promotion?: {
      master_switch_enabled: boolean;
      projection: {
        qualification_status: string;
        eligible: boolean;
        authoritative: boolean;
        blockers: string[];
      };
      championship: {
        qualification_status: string;
        eligible: boolean;
        authoritative: boolean;
        blockers: string[];
      };
    };
  };
  data_quality?: {
    evidence_gate?: {
      status: string;
      allows_reasoning: boolean;
      allows_lineup_execution: boolean;
      allows_acquisition_execution?: boolean;
      blockers: string[];
      warnings: string[];
      checks: Array<{ id: string; label: string; status: string; reason?: string | null }>;
    };
    weather_status?: string;
    weather_as_of?: string | null;
    weather_coverage?: {
      eligible_game_count?: number;
      forecast_count?: number;
      skipped_indoor_game_count?: number;
      risk_counts?: Record<string, number>;
    };
    weekly_projection_feed?: string;
    waiver_shortlist_count?: number;
    waiver_shortlist_acquisition_typed_count?: number;
    waiver_shortlist_missing_acquisition_player_ids?: string[];
    usage_authority?: string;
  };
  championship_outlook?: {
    status: string;
    model_version?: string;
    simulation_count?: number;
    projection_coverage?: number;
    structural_coverage?: number;
    modeled_replacement_share?: number;
    inferred_projection_share?: number;
    blockers?: string[];
    roster_move_counterfactuals?: {
      status: string;
      reason?: string;
      simulation_count_per_scenario: number;
      candidates: Array<{
        add_player_id: string;
        add_player_name: string;
        drop_player_id: string;
        drop_player_name: string;
        acquisition_type?: string;
        projection_proxy_delta: number;
        championship_probability_delta: number;
        playoff_probability_delta: number;
        approximate_95_percent_margin: number;
        strategically_equivalent: boolean;
      }>;
    };
    decision_features?: Record<string, {
      implemented: boolean;
      qualification_status: string;
      [key: string]: unknown;
    }>;
    model_diagnostics?: {
      live_current_week?: {
        league_fixed_points?: number;
        league_remaining_mean?: number;
        league_player_states?: Record<string, number>;
        our_team?: {
          fixed_points?: number;
          remaining_mean?: number;
          player_states?: Record<string, number>;
        };
      };
      modeled_replacement_slots?: number;
      inferred_projection_slots?: number;
      availability_scenarios?: number;
      opponent_adjusted_slots?: number;
    };
    our_team?: {
      championship_probability: number;
      playoff_probability: number;
      first_round_bye_probability: number;
      championship_standard_error: number;
      current_week_win_probability?: number | null;
    };
  };
  error?: string;
  our_team?: { all_players?: ManagerPlayer[] };
  free_agent_candidates?: ManagerPlayer[];
  lineup_execution?: {
    state: string;
    reason?: string;
    queued: Array<{
      action_id: string;
      command_id: string;
      sequence: number;
      not_before: string;
      expires_at: string;
      status: string;
    }>;
    preview_count?: number;
  };
  acquisition_execution?: {
    state: string;
    reason?: string;
    queued: Array<{
      action_id: string;
      command_id: string;
      sequence?: number;
      not_before: string;
      expires_at: string;
      status: string;
    }>;
    preview_count?: number;
    deferred_count?: number;
    continuation_policy?: "verify_reconcile_replan" | "none";
    deferred?: Array<{
      sequence: number;
      action_type: "ADD_FREE_AGENT";
      add_player_id: string;
      drop_player_id: string;
      reason: string;
    }>;
  };
  trade_market?: {
    status: string;
    as_of?: string | null;
    active_offers: Array<{
      offer_fingerprint: string;
      direction: "incoming" | "outgoing" | "unknown";
      counterparty_roster_id?: number | null;
      counterparty_account_label: string;
      receive_assets: ManagerPlayer[];
      send_assets: ManagerPlayer[];
    }>;
  };
  trade_execution_preview?: Array<{
    action_type: "PROPOSE_TRADE" | "ACCEPT_TRADE" | "DECLINE_TRADE";
    reason: string;
    confidence: number;
    parameters: {
      counterparty_roster_id: number;
      counterparty_account_label: string;
      send_assets: Array<{ player_id: string; player_name: string }>;
      receive_assets: Array<{ player_id: string; player_name: string }>;
      expiration_label?: string;
      offer_fingerprint?: string;
    };
  }>;
  trade_execution?: {
    state: string;
    reason?: string;
    preview_count?: number;
    queued: Array<{
      action_id: string;
      command_id: string;
      status: string;
      action_type: "PROPOSE_TRADE" | "ACCEPT_TRADE" | "DECLINE_TRADE";
      expires_at: string;
    }>;
    anti_spam?: {
      max_proposals_per_week: number;
      cooldown_days: number;
      outgoing_active: number;
    };
  };
  ir_plan?: {
    status: string;
    reason?: string;
    reserve_capacity: number;
    reserve_occupied: number;
    actions: Array<{
      sequence: number;
      action_type: "MOVE_TO_IR" | "REMOVE_FROM_IR";
      reason: string;
      parameters: {
        player_id: string;
        player_name: string;
        observed_status: string;
        expected_reserve_before: string[];
        expected_reserve_after: string[];
      };
    }>;
    blocked: Array<{
      player_id: string;
      player_name: string;
      action_type: "MOVE_TO_IR" | "REMOVE_FROM_IR";
      reason: string;
    }>;
  };
  ir_execution?: {
    state: string;
    reason?: string;
    queued: Array<{
      sequence: number;
      action_id: string;
      command_id: string;
      status: string;
      not_before: string;
      expires_at: string;
    }>;
  };
  lineup_execution_preview?: Array<{
    sequence: number;
    action_type: "SET_LINEUP";
    reason: string;
    parameters: {
      from_player_id: string;
      from_player_name: string;
      from_slot: string;
      to_player_id: string;
      to_player_name: string;
      to_slot: string;
      target_slot_index: number;
      expected_starters_before: string[];
      expected_starters_after: string[];
    };
  }>;
  waiver_execution_preview?: Array<{
    sequence: number;
    action_type: "WAIVER_CLAIM" | "ADD_FREE_AGENT";
    reason: string;
    parameters: {
      add_player_id: string;
      add_player_name: string;
      add_player_position: string;
      add_player_team: string;
      drop_player_id: string;
      drop_player_name: string;
      drop_player_search_name: string;
      drop_player_position: string;
      drop_player_team: string;
      claim_priority: number;
      faab_percent: number;
      contingency: string;
      horizon: "one_week_rental" | "multi_week_bridge" | "rest_of_season" | "playoff_stash";
      expected_roster_role: string;
      immediate_case: string;
      season_case: string;
      drop_cost: string;
      exit_plan: string;
      reevaluate_after_week: number;
      observed_acquisition_type: "waiver" | "free_agent";
      transaction_week: number;
      expected_roster_before: string[];
      expected_roster_after: string[];
    };
  }>;
  expert?: {
    provider?: string;
    model?: string;
    used_web_search?: boolean;
    cached?: boolean;
    latency_ms?: number;
  };
  decision?: {
    decision_status: string;
    week: number;
    confidence: number;
    team_assessment: {
      championship_outlook: string;
      weekly_strategy: string;
      strengths: string[];
      vulnerabilities: string[];
    };
    lineup_status: string;
    lineup: Array<{ slot_index: number; slot: string; player_id: string; reason: string }>;
    lineup_changes: Array<{ slot_index: number; slot: string; out_player_id: string; in_player_id: string; reason: string }>;
    waiver_status: string;
    waiver_strategy: string;
    waiver_horizon: {
      roster_thesis: string;
      rest_of_season_priorities: string[];
      playoff_priorities: string[];
      churn_policy: string;
    };
    waiver_claims: Array<{
      priority: number;
      add_player_id: string;
      drop_player_id: string;
      faab_percent: number;
      claim_type: string;
      horizon: "one_week_rental" | "multi_week_bridge" | "rest_of_season" | "playoff_stash";
      expected_roster_role: string;
      immediate_case: string;
      season_case: string;
      drop_cost: string;
      exit_plan: string;
      reevaluate_after_week: number;
      contingency: string;
      reason: string;
    }>;
    pending_waiver_management: {
      status: "keep" | "reorder" | "cancel" | "replace" | "needs_data";
      desired_order: Array<{ add_player_id: string; drop_player_id: string }>;
      reason: string;
      replacement_trigger: string;
    };
    watchlist: Array<{ player_id: string; trigger: string; reason: string }>;
    trade_status: "ready" | "watch" | "no_action" | "needs_data";
    trade_strategy: string;
    trade_targets: Array<{
      target_roster_id: number;
      target_player_ids: string[];
      offer_player_ids: string[];
      championship_case: string;
      counterparty_case: string;
      cost_ceiling: string;
      timing_trigger: string;
      risk: string;
      reason: string;
      recommended_action: "propose" | "watch";
      upside_tier: "low" | "medium" | "high";
      evidence_status: "confirmed" | "conditional" | "insufficient";
      confidence: number;
    }>;
    incoming_trade_decisions: Array<{
      offer_fingerprint: string;
      action: "accept" | "decline" | "watch";
      upside_tier: "low" | "medium" | "high";
      evidence_status: "confirmed" | "conditional" | "insufficient";
      confidence: number;
      championship_case: string;
      reason: string;
    }>;
    urgent_alerts: string[];
    evidence_gaps: string[];
    research_evidence: Array<{
      player_id: string;
      source_name: string;
      source_url: string;
      published_at: string;
      finding: string;
      decision_impact: string;
    }>;
  } | null;
};

export type AcquisitionLifecycle = {
  generated_at?: string | null;
  status: string;
  active_count: number;
  completed_count: number;
  failed_count: number;
  requires_manager_refresh: boolean;
  claims: Array<{
    command_id: string;
    action_type: string;
    outcome_status: string;
    add_player_name: string;
    drop_player_name?: string | null;
    claim_priority?: number | null;
    transaction_id?: string | null;
    transaction_notes?: string | null;
  }>;
};

export type ManagerNotification = {
  id: string;
  action_id?: string | null;
  channel: string;
  subject: string;
  body: string;
  severity: string;
  status: string;
  created_at: string;
};

export type DraftCandidate = {
  player_id: string;
  player_name: string;
  position: string;
  team?: string;
  overall_rank: number;
  position_rank?: number;
  market_adp?: number;
  draft_value_rank?: number;
  score: number;
  confidence: number;
  bye_week?: number;
  available_at_pick_probability?: number;
  survival_probability: number;
  injury_status?: string;
  injury_detail?: string;
  depth_chart_order?: number;
  replacement_player_name?: string;
  replacement_draft_value_rank?: number;
  replacement_availability_probability?: number;
  expected_position_demand_before_next_pick?: number;
  cost_of_waiting?: number;
  championship_ceiling?: number;
  championship_win_probability?: number;
  championship_equity_delta?: number;
  championship_simulations?: number;
  reasons: string[];
  score_components?: Record<string, number>;
  ranking_confidence?: number;
  decision_source?:
    | "expert_model"
    | "expert_model_alternative"
    | "programmatic_preview"
    | "programmatic_fallback_display";
};

export type ExpertDecision = {
  provider: "codex_cli" | "openai_api";
  model: string;
  response_id: string;
  confidence: number;
  roster_strategy: string;
  strategy_state?: string;
  fit_summary: string;
  roster_risk?: string;
  next_two_turn_plan?: string;
  key_tradeoffs: string[];
  grounding_hash: string;
  latency_ms: number;
  used_web_search: boolean;
  cached: boolean;
  prompt_version: string;
};

export type DraftRoom = {
  generated_at?: string;
  draft_id?: string;
  status?: string;
  draft_name?: string;
  owner_slot?: number;
  owner_roster_id?: number;
  on_clock?: boolean;
  pick_count?: number;
  current_pick_no?: number;
  next_owner_pick?: number;
  following_owner_pick?: number;
  picks_until_on_clock?: number;
  clock?: {
    started_at?: string;
    deadline?: string;
    auto_submit_at?: string;
    seconds_remaining?: number;
    auto_submit_seconds_remaining: number;
    auto_submit_delay_seconds?: number;
    submit_policy?: "mock_delay" | "live_seconds_remaining";
    source?: "last_picked" | "start_time";
  };
  format?: {
    type?: string;
    teams?: number;
    rounds?: number;
    scoring?: string;
    pick_timer_seconds?: number;
  };
  recommendation?: {
    recommendation?: DraftCandidate;
    fallbacks?: DraftCandidate[];
    engine_version?: string;
    decision_source?: "expert_model" | "programmatic_preview";
    expert_decision?: ExpertDecision;
    roster_summary?: {
      counts: Record<string, number>;
      targets: Record<string, number>;
      starters: Record<string, number>;
      flex_slots: number;
      rounds: number;
      remaining_picks: number;
      championship_model?: {
        version: string;
        simulations_per_candidate?: number;
        objective: "first_place_probability";
      };
    };
  } | null;
  expert_manager?: {
    enabled: boolean;
    provider: "codex_cli" | "openai_api";
    model: string;
    state:
      | "waiting_for_clock"
      | "reasoning"
      | "selected"
      | "blocked"
      | "disabled"
      | "not_configured"
      | "complete";
    objective: string;
    uses_web_search: boolean;
    error?: string;
    decision?: ExpertDecision;
  };
  slot_plan?: {
    owner_slot: number;
    teams: number;
    rounds: number;
    pick_numbers: number[];
    target_counts: Record<string, number>;
    projected_counts: Record<string, number>;
    target_complete: boolean;
    strategy: string;
    availability_note: string;
    projected_roster: Array<{
      round: number;
      pick: number;
      player_id: string;
      player_name: string;
      position: string;
      team?: string;
      market_adp?: number;
      available_at_pick_probability?: number;
      availability_band?: string;
      injury_status?: string;
    }>;
    round_plan: Array<Record<string, unknown>>;
  };
  autopick?: {
    enabled?: boolean;
    state?: "blocked" | "armed" | "scheduled" | "queued" | "complete";
    message?: string;
    candidate?: string;
    action_id?: string;
    command_id?: string;
    submit_at?: string;
    expires_at?: string;
  };
  execution_blockers?: string[];
  provenance?: {
    latest_source_date?: string;
    market_source_date?: string;
    source?: string;
    underlying_source?: string;
  };
  live?: {
    status?: string;
    pick_count: number;
    picks: Array<Record<string, unknown>>;
  };
  recent_picks?: Array<{
    pick_no?: number;
    player_id?: string;
    player_name?: string;
    position?: string;
    team?: string;
    is_mine?: boolean;
  }>;
  simulations?: Array<{ slot: number; picks: Array<Record<string, unknown>> }>;
};
