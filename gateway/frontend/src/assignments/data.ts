/**
 * What the JSON routes of the assignments send
 * (`viewer_api/assignments.py`).
 *
 * Every string a person reads is already a string here: the duration of a
 * run, the phrase of its start, the label of its status, the text of a
 * notice. The page renders them and counts nothing of its own.
 */

import type { DocBlock } from "../blocks/Blocks";
import type { DocComment } from "../comments/CommentCard";

/** One state of the pills: `draft`, `in_progress`, `completed`. */
export interface State {
  key: string;
  label: string;
}

/** One row of `GET /api/v1/assignments`. */
export interface AssignmentRow {
  id: number;
  url: string;
  title: string;
  state: string;
  status: string;
  status_label: string;
  when: string;
  duration: string;
  cost: string;
  turns: number | null;
  summary_line: string;
  asks: boolean;
  start_phrase: string;
}

/** The answer of `GET /api/v1/assignments`. */
export interface AssignmentList {
  configured: boolean;
  available: boolean;
  state: string | null;
  states: State[];
  items: AssignmentRow[];
  empty: string;
  timezone: string;
  now: string;
}

/** The tokens of a run. */
export interface Usage {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
  total: number;
}

/** One note of the worker, newest first. */
export interface Checkpoint {
  at: string | null;
  when: string;
  note: string;
}

/** One thing the assignment made. */
export interface Output {
  kind: string;
  ref: string;
  title: string;
  when: string;
}

/** What the assignment waits for, while it waits. */
export interface OpenRequest {
  kind: string;
  ref: string;
  reason: string;
}

/** The notice of the page: a request, a question, or the limits. */
export interface Notice {
  kind: string;
  heading: string;
  when: string;
  ref: string;
  text: string;
  foot: string;
}

/** One run of the lineage of the assignment. */
export interface LineageRun {
  id: number;
  url: string;
  title: string;
  status: string;
  status_label: string;
  when: string;
  current: boolean;
}

/** The assignment itself. */
export interface AssignmentItem {
  id: number;
  title: string;
  state: string;
  status: string;
  status_label: string;
  when: string;
  model: string;
  usage: Usage;
  cost: string;
  cost_title: string;
  duration: string;
  turns: number | null;
  started_at: string | null;
  finished_at: string | null;
  error: string;
  open_comments: number;
  start_phrase: string;
  draft_line: string;
  notice: Notice | null;
  checkpoints: Checkpoint[];
  outputs: Output[];
  open_request: OpenRequest | null;
  lineage: LineageRun[];
  run_number: { run: number; root: number } | null;
  saved_ref: { id: number; name: string } | null;
}

/** The value of one field of the definition, as the page draws it. */
export interface PropValue {
  kind: string;
  text?: string;
  tags?: string[];
}

/**
 * One commentable part of the definition: a field (`prop`), the head of the
 * goal (`goal`), or one block of the goal (`p`, `li`, `code`, …).
 */
export interface DefinitionUnit {
  id: string;
  first_line: number;
  last_line: number;
  kind: string;
  label: string;
  value: PropValue | null;
  markdown: string;
  depth: number;
  what: string;
}

/** The definition: its units, its lines, and the line of its `goal:` key. */
export interface DefinitionData {
  units: DefinitionUnit[];
  lines: string[];
  goal_offset: number;
}

/** How far the task boxes of the plan are. */
export interface PlanProgress {
  done: number;
  total: number;
  next: string;
  percent: number;
}

/** The plan: its blocks, its lines, and its progress. */
export interface PlanData {
  markdown: string;
  lines: string[];
  blocks: DocBlock[];
  updated: string;
  progress: PlanProgress;
}

/**
 * One item of a section of a report: a highlight, a decision, or a
 * follow-up. `line` is its line in the section, and `first_line` and
 * `last_line` are the lines of the whole text it holds, so the card is a
 * unit of the comment gutter.
 */
export interface ReportItem {
  number: number;
  text: string;
  lead: string;
  body: string;
  line: number;
  first_line: number;
  last_line: number;
  question: string;
  context: string;
  /** The text a person copies for Joshua, on a decision of a document. */
  copy: string;
}

/** One line of the Outputs section, as a card. */
export interface ReportOutput {
  kind: string;
  /** The name of the icon of the kind: `page`, `issue`, …, `other`. */
  icon: string;
  ref: string;
  title: string;
  note: string;
  url: string | null;
  line: number;
  first_line: number;
  last_line: number;
}

/** One section of a report, by its normalized title. */
export interface ReportSection {
  key: string;
  title: string;
  body: string;
  line: number;
  start: number;
  end: number;
  empty: boolean;
  items: ReportItem[];
  outputs: ReportOutput[];
  rest: string;
  blocks: DocBlock[];
}

/** The text of a report before its first heading, after the lede. */
export interface ReportPreamble {
  markdown: string;
  line: number;
  blocks: DocBlock[];
}

/**
 * A text of the report shape, as the showcase reads it
 * (`viewer_api.assignments.report_json`). With `structured` false the text
 * has none of the headings, and the page shows its blocks.
 */
export interface ShowcaseData {
  structured: boolean;
  title: string;
  lede: string;
  generated: boolean;
  first_section: number;
  preamble: ReportPreamble | null;
  sections: Record<string, ReportSection>;
  extra: ReportSection[];
  turns: number | null;
}

/** The report, when the assignment wrote one, with the text itself. */
export interface ReportData extends ShowcaseData {
  markdown: string;
  lines: string[];
  blocks: DocBlock[];
  updated: string;
}

/** The headings of the report shape, as `viewer_report` normalizes them. */
export const HIGHLIGHTS = "highlights";
export const ASKED = "what was asked";
export const DONE = "what was done";
export const OUTPUTS = "outputs";
export const DECISIONS = "decisions for you";
export const FOLLOW_UPS = "follow-ups";
export const COST = "cost";

/** The comments of the assignment, by the text each one is on. */
export interface CommentSet {
  definition: DocComment[];
  plan: DocComment[];
  report: DocComment[];
}

/** The answer of `GET /api/v1/assignments/<id>`. */
export interface AssignmentData {
  configured: boolean;
  available: boolean;
  not_found: boolean;
  item: AssignmentItem | null;
  definition: DefinitionData | null;
  plan: PlanData | null;
  report: ReportData | null;
  comments: CommentSet;
  csrf: string | null;
  refresh_seconds: number | null;
  timezone: string;
  now: string;
}

/** The three texts of an assignment that take a comment. */
export const TARGETS: readonly (keyof CommentSet)[] = ["definition", "plan", "report"];

/** No comment on any text, which a page with no assignment holds. */
export const NO_COMMENTS: CommentSet = { definition: [], plan: [], report: [] };

/** The last run of a saved assignment, when it ran once. */
export interface LastRun {
  id: number;
  url: string;
  status: string;
  status_label: string;
  when: string;
}

/** One row of `GET /api/v1/assignments/saved`. */
export interface SavedRow {
  id: number;
  url: string;
  name: string;
  title: string;
  schedule: string;
  last_run: LastRun | null;
  run_count: number;
  open_comments: number;
}

/** The answer of `GET /api/v1/assignments/saved`. */
export interface SavedList {
  configured: boolean;
  available: boolean;
  items: SavedRow[];
  empty: string;
  timezone: string;
  now: string;
}

/** One run of a saved assignment, with its number of that saved one. */
export interface SavedRun extends AssignmentRow {
  number: number;
}

/** The answer of `GET /api/v1/assignments/saved/<id>`. */
export interface SavedData {
  configured: boolean;
  available: boolean;
  not_found: boolean;
  item: (SavedRow & { updated: string }) | null;
  definition: DefinitionData | null;
  runs: SavedRun[];
  comments: DocComment[];
  /** What takes a comment: the definition, and nothing else. */
  targets: string[];
  csrf: string | null;
  timezone: string;
  now: string;
}
