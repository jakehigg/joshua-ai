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

/** One section of a report, by its normalized title. */
export interface ReportSection {
  key: string;
  title: string;
  body: string;
  line: number;
  start: number;
  end: number;
}

/** The report, when the assignment wrote one. */
export interface ReportData {
  markdown: string;
  lines: string[];
  blocks: DocBlock[];
  updated: string;
  title: string;
  lede: string;
  structured: boolean;
  sections: Record<string, ReportSection>;
}

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
