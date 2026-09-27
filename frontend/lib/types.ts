/**
 * Response shapes, mirrored from the API's Pydantic models.
 *
 * Hand-written rather than generated: the generator would be another build step
 * and another dependency, and the API publishes an OpenAPI document at
 * /openapi.json for anybody who wants to generate their own.
 */

/** Mirrors `app/pagination.py`'s `Page[T]` -- every list endpoint big enough to
 * need paging returns exactly this shape, so one type and one set of helper
 * links covers all of them. `GalleryPage` predates this and keeps its own
 * type; it already worked before this existed. */
export type Page<T> = {
  items: T[];
  total: number;
  page: number;
  per_page: number;
  pages: number;
};

export type Role = 'visitor' | 'participant' | 'judge' | 'organizer' | 'admin';

export const ROLE_RANK: Record<Role, number> = {
  visitor: 0,
  participant: 1,
  judge: 2,
  organizer: 3,
  admin: 4,
};

/** Hides buttons. Never a control -- the API decides, every time. */
export function atLeast(role: Role, floor: Role): boolean {
  return ROLE_RANK[role] >= ROLE_RANK[floor];
}

export type UserPublic = {
  id: string;
  display_name: string;
  role: Role;
};

export type AdminLevel = 'owner' | 'manager' | 'auditor';

export type User = UserPublic & {
  email: string;
  is_active: boolean;
  created_at: string;
  /** Only an admin has one; null for every other role. */
  admin_level: AdminLevel | null;
};

export type Me = {
  authenticated: boolean;
  user: User | null;
  role: Role;
};

export type Track = {
  id: string;
  key: string;
  name: string;
  description: string | null;
  position: number;
};

export type Prize = {
  id: string;
  title: string;
  description: string | null;
  value: string | null;
  track_id: string | null;
  position: number;
};

export type Question = {
  id: string;
  prompt: string;
  help_text: string | null;
  kind: 'text' | 'textarea' | 'url' | 'select' | 'checkbox';
  options: string[] | null;
  required: boolean;
  position: number;
};

export type EventSummary = {
  id: string;
  slug: string;
  name: string;
  tagline: string | null;
  starts_at: string;
  ends_at: string;
  submission_deadline: string;
  is_published: boolean;
  /** Set while the event is archived: frozen and read-only. */
  archived_at: string | null;
};

export type Event = EventSummary & {
  description: string | null;
  website_url: string | null;
  registration_opens_at: string;
  submission_opens_at: string;
  judging_opens_at: string | null;
  judging_closes_at: string | null;
  voting_opens_at: string | null;
  voting_closes_at: string | null;
  voting_access: VotingAccess;
  voting_method: VotingMethod;
  vote_credits: number;
  votes_per_voter: number;
  comments_enabled: boolean;
  /** The Gavel-style alternative to the scored rubric. Additive: an event may
   * run this and the rubric together. */
  pairwise_enabled: boolean;
  /** The window compared against the server clock, so the UI does no date maths. */
  voting_open: boolean;
  results_public_at: string | null;
  results_public: boolean;
  /** Top `winner_slots` ranks by judge score are outright winners; the next
   * `community_vote_slots` move to a scoped community-voting round. Both 0
   * means this event does not use the split. */
  winner_slots: number;
  community_vote_slots: number;
  max_team_size: number;
  tracks: Track[];
  prizes: Prize[];
  questions: Question[];
  submissions_open: boolean;
  registration_open: boolean;
};

export type TeamMember = {
  user: UserPublic;
  team_role: 'owner' | 'member';
  joined_at: string;
};

export type Team = {
  id: string;
  event_id: string;
  event_slug: string;
  name: string;
  created_at: string;
  members: TeamMember[];
  submission_id: string | null;
};

export type Registration = {
  id: string;
  event_id: string;
  user_id: string;
  user_display_name: string;
  email: string;
  discord_username: string;
  is_team_leader: boolean;
  leader_name: string | null;
  registered_at: string;
  has_team: boolean;
};

export type Announcement = {
  id: string;
  event_id: string;
  title: string;
  body: string;
  visible_to_visitors: boolean;
  posted_by_display_name: string | null;
  posted_at: string;
};

export type Invite = { token: string; url: string };

export type Answer = { question_id: string; prompt: string; value: string | null };

export type SubmissionCard = {
  id: string;
  name: string;
  tagline: string | null;
  thumbnail_url: string | null;
  tech_tags: string[];
  track: Track | null;
  team_name: string;
  submitted_at: string | null;
};

export type Submission = {
  id: string;
  event_id: string;
  event_slug: string;
  team_id: string;
  team_name: string;
  name: string;
  tagline: string | null;
  description: string | null;
  thumbnail_url: string | null;
  gallery_image_urls: string[];
  demo_video_url: string | null;
  repo_url: string | null;
  live_url: string | null;
  linkedin_url: string | null;
  tech_tags: string[];
  discord_usernames: string[];
  track: Track | null;
  status: 'draft' | 'submitted' | 'disqualified';
  submitted_at: string | null;
  created_at: string;
  updated_at: string;
  answers: Answer[];
  members: UserPublic[];
  can_edit: boolean;
  can_submit: boolean;
  /** Part 3's winner/community-vote split -- null unless the event uses it
   * and (for a non-staff caller) results are public. Only ever set by
   * `GET /submissions/{id}`. */
  tier: 'winner' | 'community_tier' | null;
};

export type GalleryPage = {
  items: SubmissionCard[];
  total: number;
  page: number;
  per_page: number;
  pages: number;
};


// --------------------------------------------------------------------------- //
// Judging (Phase 2)
// --------------------------------------------------------------------------- //

export type AssignmentStatus = 'pending' | 'in_progress' | 'complete';

export type Criterion = {
  id: string;
  key: string;
  name: string;
  description: string | null;
  /** As the organizer typed it. Divided by the sum of weights at scoring time. */
  weight: number;
  min_score: number;
  max_score: number;
  position: number;
};

export type Judge = {
  id: string;
  event_id: string;
  event_slug: string;
  pairwise_enabled: boolean;
  user: UserPublic;
  /** Staff-only endpoint, so the address is carried for the invite list. */
  email: string;
  /** null means this judge sees every track. */
  track: Track | null;
  is_active: boolean;
  invited_at: string;
  accepted_at: string | null;
};

export type Score = {
  criterion_id: string;
  criterion_key: string;
  value: number;
  comment: string | null;
};

export type Assignment = {
  id: string;
  event_id: string;
  event_slug: string;
  judge_id: string;
  judge_name: string;
  status: AssignmentStatus;
  comment: string | null;
  assigned_at: string;
  completed_at: string | null;
  submission: SubmissionCard;
  scores: Score[];
  raw_score: number | null;
  /** Computed by the same predicate that enforces it. A courtesy, not a control. */
  can_score: boolean;
};

export type Shortfall = {
  submission_id: string;
  submission_name: string;
  requested: number;
  achieved: number;
  eligible: number;
  reason: string;
};

export type AssignResult = {
  created: number;
  reviews_per_submission: number;
  submissions: number;
  judges: number;
  balanced: boolean;
  spread: number;
  loads: Record<string, number>;
  shortfalls: Shortfall[];
  dry_run: boolean;
};

export type JudgeProgress = {
  judge_id: string;
  judge_name: string;
  track: string | null;
  assigned: number;
  complete: number;
  in_progress: number;
  pending: number;
};

export type Progress = {
  event_slug: string;
  judging_opens_at: string | null;
  judging_closes_at: string | null;
  judging_open: boolean;
  judges: JudgeProgress[];
  submissions_total: number;
  submissions_fully_reviewed: number;
  assignments_total: number;
  assignments_complete: number;
  percent_complete: number;
  not_started: string[];
};

export type Calibration = {
  judge_id: string;
  judge_name: string;
  /** `null` is the untracked-submissions group, not "unknown". A judge who
   * reviews across every track appears once per track they scored in. */
  track: string | null;
  n: number;
  mean: number;
  sd: number;
  shrunk_mean: number;
  shrunk_sd: number;
  /** Marked everything the same value; contributes no ordering. */
  flat: boolean;
  note: string;
};

export type ResultRow = {
  submission_id: string;
  submission_name: string;
  team_name: string;
  track: string | null;
  n_reviews: number;
  raw_mean: number;
  normalized_mean: number;
  raw_rank: number;
  normalized_rank: number;
  /** Positive means normalization moved this project up the table. */
  rank_delta: number;
  /** Part 3's winner/community-vote split, by `normalized_rank` -- null for
   * every row on an event that does not use it. Untouched by an override:
   * the judge-computed answer, always. */
  computed_tier: 'winner' | 'community_tier' | null;
  /** Part 4: `computed_tier` with an admin override applied, if any -- the
   * one voting eligibility and a team's own project page actually use. */
  tier: 'winner' | 'community_tier' | null;
  override_reason: string | null;
  overridden_by: string | null;
  overridden_at: string | null;
};

export type ScoringGridCell = { criterion_id: string; value: number };

export type ScoringGridJudgeRow = {
  judge_id: string;
  judge_name: string;
  status: string;
  is_adjudication: boolean;
  scores: ScoringGridCell[];
  comment: string | null;
  raw_mean: number | null;
  normalized_mean: number | null;
};

export type ScoringGrid = {
  submission_id: string;
  submission_name: string;
  criteria: Criterion[];
  judges: ScoringGridJudgeRow[];
  n_reviews: number;
  final_raw_mean: number | null;
  final_normalized_mean: number | null;
};

export type JudgeReportRow = {
  /** "Judge 1", "Judge 2", ... -- never a real name; see the backend
   * schema's own docstring for why. */
  label: string;
  scores: ScoringGridCell[];
  comment: string | null;
  raw_mean: number | null;
  normalized_mean: number | null;
};

export type JudgeReport = {
  submission_id: string;
  submission_name: string;
  criteria: Criterion[];
  judges: JudgeReportRow[];
  n_reviews: number;
  final_raw_mean: number | null;
  final_normalized_mean: number | null;
  tier: 'winner' | 'community_tier' | null;
};

export type PublicResultRow = {
  submission_id: string;
  submission_name: string;
  team_name: string;
  track: string | null;
  tier: 'winner' | 'community_tier' | null;
  normalized_rank: number | null;
  normalized_score: number | null;
  thumbnail_url: string | null;
  repo_url: string | null;
  live_url: string | null;
  demo_video_url: string | null;
};

export type PublicResults = {
  event_slug: string;
  rows: PublicResultRow[];
};

export type Results = {
  event_slug: string;
  method: string;
  global_mean: number;
  global_sd: number;
  shrinkage_k: number;
  rows: ResultRow[];
  calibrations: Calibration[];
};

// --------------------------------------------------------------------------- //
// Judging integrity: disagreement and consistency
// --------------------------------------------------------------------------- //

export type DisagreementRow = {
  submission_id: string;
  submission_name: string;
  team_name: string;
  track: string | null;
  n_reviews: number;
  sd: number;
  mean_abs_pairwise_diff: number;
  needs_review: boolean;
};

export type JudgeDeviation = {
  judge_id: string;
  judge_name: string;
  n_reviews: number;
  mean_abs_deviation: number;
};

export type Disagreement = {
  threshold: number;
  needs_review_count: number;
  submissions: DisagreementRow[];
  judges: JudgeDeviation[];
};

export type ConsistencyFlag = {
  judge_id: string;
  judge_name: string;
  reason: string;
  detail: string;
};

export type Consistency = {
  flags: ConsistencyFlag[];
};

// --------------------------------------------------------------------------- //
// Pairwise judging (Phase 5)
// --------------------------------------------------------------------------- //

export type PairwiseResultRow = {
  submission_id: string;
  submission_name: string;
  team_name: string;
  track: string | null;
  rank: number;
  rating: number;
  strength: number;
  n_comparisons: number;
  wins: number;
  losses: number;
  win_rate: number | null;
};

export type PairwiseJudgeProgress = {
  judge_id: string;
  judge_name: string;
  n_comparisons: number;
};

export type PairwiseCoverage = {
  total_submissions: number;
  total_comparisons: number;
  min_comparisons: number;
  max_comparisons: number;
  mean_comparisons: number;
  coverage_pct: number;
};

export type PairwiseResults = {
  event_slug: string;
  method: string;
  total_comparisons: number;
  converged: boolean;
  coverage: PairwiseCoverage;
  rows: PairwiseResultRow[];
  judges: PairwiseJudgeProgress[];
};

export type PairwiseComparison = {
  id: string;
  submission_a_id: string;
  submission_b_id: string;
  winner_id: string | null;
  created_at: string;
};

export type Pair = {
  submission_a: Submission;
  submission_b: Submission;
};


// --------------------------------------------------------------------------- //
// Public voting, comments and audit (Phase 3)
// --------------------------------------------------------------------------- //

export type VotingAccess = 'open_link' | 'email_gated' | 'authenticated';
export type VotingMethod = 'single' | 'quadratic';

export type Voter = {
  id: string;
  event_slug: string;
  access: VotingAccess;
  method: VotingMethod;
  /** Open-link and email-gated only; the ballot identity for a caller with no account. */
  token: string | null;
  email: string | null;
  /** False in every mode but `authenticated` — no email is ever sent. */
  verified: boolean;
  caveat: string | null;
  credits_total: number;
  credits_spent: number;
  credits_remaining: number;
  max_projects: number;
  voting_open: boolean;
  voting_closes_at: string | null;
};

export type CastVote = {
  submission_id: string;
  submission_name: string;
  credits: number;
  /** `credits²` in quadratic mode. */
  cost: number;
};

export type Ballot = {
  voter_id: string;
  event_slug: string;
  method: VotingMethod;
  credits_total: number;
  credits_spent: number;
  credits_remaining: number;
  max_projects: number;
  voting_open: boolean;
  votes: CastVote[];
  /** Randomised per voter, stable across refreshes. */
  projects: SubmissionCard[];
};

export type TallyRow = {
  submission_id: string;
  submission_name: string;
  team_name: string;
  track: string | null;
  rank: number;
  votes: number;
  voters: number;
  credits: number;
};

export type Tally = {
  event_slug: string;
  method: VotingMethod;
  access: VotingAccess;
  voting_open: boolean;
  public: boolean;
  total_voters: number;
  rows: TallyRow[];
  /** What these totals can and cannot be read as, given the access mode. */
  caveat: string | null;
};

export type Comment = {
  id: string;
  submission_id: string;
  author: UserPublic;
  body: string;
  created_at: string;
  is_hidden: boolean;
  hidden_reason: string | null;
  can_remove: boolean;
};

export type AuditAction =
  | 'vote_cast'
  | 'vote_changed'
  | 'vote_withdrawn'
  | 'ballot_scored'
  | 'comment_posted'
  | 'comment_hidden'
  | 'judge_invited'
  | 'judge_deactivated'
  | 'assignment_run'
  | 'assignment_deleted'
  | 'rubric_changed'
  | 'results_published'
  | 'submission_edited_after_deadline'
  | 'role_changed'
  | 'rate_limited';

export type AuditRow = {
  id: string;
  created_at: string;
  action: AuditAction;
  actor_label: string;
  actor_role: string | null;
  resource_type: string | null;
  resource_id: string | null;
  /** A whole sentence. This is the column the page exists to show. */
  summary: string;
  ip_address: string | null;
};

export type AuditPage = {
  event_slug: string;
  total: number;
  page: number;
  per_page: number;
  pages: number;
  rows: AuditRow[];
};

// --------------------------------------------------------------------------- //
// Webhooks, certificates and import (Phase 4)
// --------------------------------------------------------------------------- //

export type Webhook = {
  id: string;
  event_id: string;
  url: string;
  /** Empty means every topic. */
  topics: string[];
  is_active: boolean;
  description: string | null;
  created_at: string;
  /** Consecutive failures; the hook is disabled automatically once this is high. */
  failure_count: number;
  last_delivery_at: string | null;
};

/** Only the create response carries the secret, and only once. */
export type WebhookCreated = Webhook & { secret: string };

export type DeliveryStatus = 'pending' | 'delivered' | 'failed' | 'blocked';

export type Delivery = {
  id: string;
  topic: string;
  status: DeliveryStatus;
  attempts: number;
  response_code: number | null;
  error: string | null;
  created_at: string;
  delivered_at: string | null;
};

export type CertificateKind = 'participation' | 'judging' | 'placement' | 'organizing';

export type Certificate = {
  id: string;
  event_id: string;
  kind: CertificateKind;
  subject_name: string;
  title: string;
  /** The public verification handle. */
  code: string;
  /** The exact bytes that were signed, not a re-serialisation. */
  payload: string;
  signature: string;
  /** Which published key (see `/api/signing/public-keys`) signed it -- Ed25519,
   * not a shared secret, so a verifier needs the specific key by id. */
  key_id: string;
  issued_at: string;
  revoked_at: string | null;
  revoked_reason: string | null;
  valid: boolean;
};

/** One name the certificate wizard may offer for a project -- never free text,
 * always a name that already has an issued certificate. */
export type CertificateLookupRow = {
  code: string;
  kind: CertificateKind;
  title: string;
  subject_name: string;
};

export type ImportResult = {
  created: number;
  updated: number;
  skipped: number;
  /** Per-row, because one typo must not discard the other thirty-nine corrections. */
  errors: string[];
};

export type CalibrationValue = { criterion_id: string; value: number };

/** What a judge sees of a practice project: never the expected scores. */
export type CalibrationPractice = {
  id: string;
  event_slug: string;
  name: string;
  description: string | null;
  criteria: Criterion[];
  my_scores: CalibrationValue[];
  complete: boolean;
};

/** Staff view of a practice project, expected scores included. */
export type CalibrationProject = {
  id: string;
  event_slug: string;
  name: string;
  description: string | null;
  expected: CalibrationValue[];
  created_at: string;
};

export type CalibrationJudgeRow = {
  judge_id: string;
  judge_name: string;
  projects_scored: number;
  projects_total: number;
  mean_signed_deviation: number | null;
  mean_abs_deviation: number | null;
  verdict: 'not_started' | 'incomplete' | 'harsh' | 'generous' | 'aligned';
};

export type CalibrationReport = {
  event_slug: string;
  threshold: number;
  projects_total: number;
  rows: CalibrationJudgeRow[];
};
