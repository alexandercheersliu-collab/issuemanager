/** 与后端 models/schemas.QuestionOut 对齐的移动端子集。 */
export interface User {
  user_id: number
  username: string
  role: string
}

export interface FigureSpec {
  validation_status: 'ai_passed' | 'manual_passed' | 'fallback'
}

export interface Question {
  id: number
  user_id: number
  image_path: string | null
  figure_paths: string[]
  figure_specs: FigureSpec[]
  content_markdown: string
  stem: string
  analysis: string | null
  answer: string
  knowledge_points: string[]
  tags: string[]
  difficulty: string
  subject: string
  grade: number | null
  error_category: string | null
  reps: number
  interval_days: number
  due_at: string | null
  last_reviewed_at: string | null
  created_at: string | null
  demotion_state: string
}

export interface DashboardStats {
  total: number
  due: number
  reviewed: number
  mastered: number
  streak: number
}

export interface AnalyzeResult {
  question: Question
  analysis: {
    knowledge_points?: string[]
    difficulty?: string
    summary?: string
    /** 题目是否拍全；false 时 completeness_note 说明缺了哪部分 */
    is_complete?: boolean
    completeness_note?: string
    /** 大题只错一道小题时，AI 提取的做错小题题干（已作为题面入库） */
    focused_sub_question?: string
  }
}

export interface PhotoCandidate {
  id: string
  image: string
  number: string
  text: string
  summary: string
  likely_wrong: boolean
  figure_bboxes: number[][]
}

export interface PhotoEntry {
  name: string
  error: string | null
  question: Question | null
  analysis: AnalyzeResult['analysis'] | null
  duplicated: boolean
}

export interface PhotoJob {
  job_id: string
  phase: 'planning' | 'review' | 'importing' | 'done' | 'failed'
  candidates: PhotoCandidate[]
  fallback_count: number
  total: number
  done: number
  entries: PhotoEntry[]
  error: string | null
}

/** 同类题检测：POST /questions/{id}/detection/start 的响应 */
export interface DetectionStart {
  log_id: number
  question_id: number
  tested: {
    source: string
    content: string
    stem: string
    figure_tokens: string[]
    figure_specs: FigureSpec[]
    knowledge_points: string[]
    difficulty: string
  }
  relax_level: number
}

/** 同类题检测：POST /detection/{log_id}/answer 的响应（判分 + 降级/回升联动） */
export interface DetectionAnswerResult {
  result: 'correct' | 'wrong'
  expected_answer: string
  streak: number
  state: string
  demoted: boolean
  promoted: boolean
  mastered: boolean
  next_interval: number
  threshold: number
}

/** 整卷导入：POST /documents/import 的响应 */
export interface DocumentImport {
  job_id: string
  duplicated: boolean
  status: string
}

/** 整卷导入：单题切分结果（与 document_service 落库的 segment entry 对齐） */
export interface Segment {
  number: string | null
  text: string
  page_start: number
  page_end: number
  needs_review: boolean
  continued: boolean
  source: string
  figure_paths: string[]
  figure_specs: FigureSpec[]
  analysis: { knowledge_points?: string[]; difficulty?: string } | null
  error: string | null
}

/** 整卷导入：GET /documents/{job_id}/segments 的响应（轮询进度 + 题目列表） */
export interface SegmentsResult {
  job_id: string
  status: 'pending' | 'running' | 'success' | 'failed'
  error: string | null
  pages: number
  scanned_pages: number
  total: number
  done: number
  needs_review_count: number
  confirmed: boolean
  segments: Segment[]
}

/** 整卷导入：POST /documents/{job_id}/confirm 的响应 */
export interface ConfirmResult {
  imported: number
  skipped: { number: string | null; reason: string }[]
  already_confirmed: boolean
}

/** AI 助手对话消息（与 /agent/chat/stream 的 history 结构一致） */
export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}
