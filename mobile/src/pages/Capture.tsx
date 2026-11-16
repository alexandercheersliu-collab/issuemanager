import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Camera, CheckCircle2, FileStack, Loader2, X } from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { api, ApiError } from '@/lib/api'
import type { PhotoCandidate, PhotoJob } from '@/types'
import AuthImage from '@/components/AuthImage'
import MathText from '@/components/MathText'
import QuestionEvidence from '@/components/QuestionEvidence'
import BatchImport, { peekPendingJob } from '@/pages/BatchImport'

const PHOTO_JOB_KEY = 'mm_photo_job'
const POLL_MS = 500

export default function Capture({ onDone }: { onDone: () => void }) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [mode, setMode] = useState<'single' | 'batch'>(() =>
    peekPendingJob() ? 'batch' : 'single',
  )
  const [files, setFiles] = useState<File[]>([])
  const [previews, setPreviews] = useState<string[]>([])
  const [tags, setTags] = useState('')
  const [hint, setHint] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [jobId, setJobId] = useState<string | null>(() => localStorage.getItem(PHOTO_JOB_KEY))
  const [job, setJob] = useState<PhotoJob | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [matchChoices, setMatchChoices] = useState<Record<string, number | null>>({})
  const [pollFailures, setPollFailures] = useState(0)

  const applyJob = useCallback((next: PhotoJob) => {
    setJob(next)
    setPollFailures(0)
    if (next.phase === 'review') {
      setSelected(
        new Set(next.candidates.filter((candidate) => candidate.likely_wrong).map((c) => c.id)),
      )
      setMatchChoices({})
    }
    if (next.phase === 'done') localStorage.removeItem(PHOTO_JOB_KEY)
  }, [])

  useEffect(() => {
    return () => previews.forEach((url) => URL.revokeObjectURL(url))
  }, [previews])

  useEffect(() => {
    if (
      !jobId ||
      job?.phase === 'review' ||
      job?.phase === 'done' ||
      job?.phase === 'failed'
    ) {
      return
    }

    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null

    async function poll() {
      try {
        const next = await api.getPhotoJob(jobId!)
        if (cancelled) return
        applyJob(next)
        if (next.phase === 'planning' || next.phase === 'importing') {
          timer = setTimeout(poll, POLL_MS)
        }
      } catch (err) {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 404) {
          localStorage.removeItem(PHOTO_JOB_KEY)
          setJobId(null)
          setJob(null)
          toast.error('上次任务已不存在，请重新上传')
          return
        }
        setPollFailures((count) => count + 1)
        timer = setTimeout(poll, POLL_MS * 2)
      }
    }

    void poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [applyJob, jobId, job?.phase])

  function pick(next: File[]) {
    setFiles(next)
    setPreviews(next.map((file) => URL.createObjectURL(file)))
  }

  async function submit() {
    if (!files.length) return
    setSubmitting(true)
    try {
      const res = await api.createPhotoJob(files, { tags: tags.trim(), hint: hint.trim() })
      localStorage.setItem(PHOTO_JOB_KEY, res.job_id)
      setSelected(new Set())
      setMatchChoices({})
      setJob(null)
      setJobId(res.job_id)
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '上传失败，请重试')
    } finally {
      setSubmitting(false)
    }
  }

  async function confirm() {
    if (!jobId) return
    setSubmitting(true)
    try {
      const choices: Record<string, number | null> = {}
      job?.candidates.forEach((candidate) => {
        if (
          selected.has(candidate.id) &&
          candidate.match_status === 'similar' &&
          Object.prototype.hasOwnProperty.call(matchChoices, candidate.id)
        ) {
          choices[candidate.id] = matchChoices[candidate.id]
        }
      })
      applyJob(await api.confirmPhotoJob(jobId, Array.from(selected), choices))
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '确认失败，请重试')
    } finally {
      setSubmitting(false)
    }
  }

  function toggleCandidate(id: string, checked: boolean) {
    setSelected((current) => {
      const next = new Set(current)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }

  function chooseMatch(candidateId: string, canonicalId: number | null) {
    setMatchChoices((current) => ({ ...current, [candidateId]: canonicalId }))
  }

  function reset() {
    localStorage.removeItem(PHOTO_JOB_KEY)
    setJobId(null)
    setJob(null)
    setSelected(new Set())
    setMatchChoices({})
    setPollFailures(0)
    pick([])
    setTags('')
    setHint('')
  }

  if (mode === 'batch') {
    return <BatchImport onBack={() => setMode('single')} onDone={onDone} />
  }

  if (job?.phase === 'done') {
    const imported = job.entries.filter(
      (entry) => !entry.error && entry.question && !entry.duplicated,
    )
    const duplicates = job.entries.filter((entry) => entry.duplicated)
    const failures = job.entries.filter((entry) => entry.error || !entry.question)
    return (
      <div className="px-4 pb-24 pt-6">
        <Card>
          <CardContent className="space-y-4 p-5 text-center">
            <CheckCircle2 className="mx-auto h-12 w-12 text-green-600" />
            <h2 className="text-lg font-bold text-slate-900">录入完成</h2>
            <div className="flex flex-wrap justify-center gap-2 text-sm">
              <Badge variant="secondary" className="bg-teal-50 text-teal-700">
                已入库 {imported.length} 道
              </Badge>
              {duplicates.length > 0 && (
                <Badge variant="secondary" className="bg-amber-50 text-amber-700">
                  重复 {duplicates.length} 道
                </Badge>
              )}
              {failures.length > 0 && (
                <Badge variant="secondary" className="bg-red-50 text-red-700">
                  失败 {failures.length} 道
                </Badge>
              )}
            </div>
            {duplicates.length > 0 && (
              <p className="text-sm text-amber-700">重复照片已复用错题本中的原有记录。</p>
            )}
            {failures.length > 0 && (
              <div className="space-y-1 rounded-lg bg-red-50 p-3 text-left text-sm text-red-700">
                {failures.map((entry) => (
                  <p key={entry.name}>
                    {entry.name}：{entry.error || '解析失败'}
                  </p>
                ))}
              </div>
            )}
            <div className="space-y-3 text-left">
              {job.entries.map((entry) => {
                const question = entry.question
                if (!question || entry.error) return null
                return (
                  <div key={entry.name} className="space-y-2 rounded-lg border border-slate-200 p-3">
                    <p className="text-xs font-medium text-slate-500">{entry.name}</p>
                    <MathText text={question.stem} className="text-sm text-slate-800" />
                    <QuestionEvidence
                      question={question}
                      imageAlt={`本次私有原图：${entry.name}`}
                    />
                    {question.figure_paths.map((_, index) => (
                      <AuthImage
                        key={index}
                        src={`/questions/${question.id}/figures/${index}`}
                        alt={`共享 AI 重绘图 ${index + 1}：${entry.name}`}
                      />
                    ))}
                  </div>
                )
              })}
            </div>
            <div className="flex gap-3 pt-2">
              <Button variant="outline" className="flex-1" onClick={reset}>
                再录一道
              </Button>
              <Button className="flex-1" onClick={onDone}>
                去错题本看看
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    )
  }

  if (job?.phase === 'failed') {
    return (
      <div className="px-4 pb-24 pt-6">
        <Card>
          <CardContent className="space-y-4 p-5 text-center">
            <AlertTriangle className="mx-auto h-12 w-12 text-amber-600" />
            <h2 className="text-lg font-bold text-slate-900">录入失败</h2>
            <p className="text-sm text-slate-600">{job.error || '任务处理失败，请重新上传'}</p>
            <Button className="w-full" onClick={reset}>
              重新上传
            </Button>
          </CardContent>
        </Card>
      </div>
    )
  }

  if (job?.phase === 'review') {
    const unresolvedSimilar = job.candidates.some(
      (candidate) =>
        selected.has(candidate.id) &&
        candidate.match_status === 'similar' &&
        !Object.prototype.hasOwnProperty.call(matchChoices, candidate.id),
    )
    return (
      <div className="px-4 pb-32 pt-6">
        <header className="mb-4">
          <h1 className="text-xl font-bold text-slate-900">确认识别结果</h1>
          <p className="text-sm text-slate-500">
            从照片中识别出 {job.candidates.length} 道题，请勾选要录入的题目
          </p>
          {job.fallback_count > 0 && (
            <p className="mt-1 text-xs text-slate-400">
              另有 {job.fallback_count} 张单题照片将自动录入
            </p>
          )}
        </header>
        <div className="space-y-3">
          {job.candidates.map((candidate) => (
            <Card key={candidate.id}>
              <CardContent className="flex items-start gap-3 p-4">
                <Checkbox
                  className="mt-1"
                  checked={selected.has(candidate.id)}
                  onCheckedChange={(checked) => toggleCandidate(candidate.id, checked === true)}
                  aria-label={`选择 ${candidate.image} 第 ${candidate.number} 题`}
                />
                <div className="min-w-0 flex-1 space-y-2">
                  <div className="flex flex-wrap items-center gap-1.5 text-xs text-slate-500">
                    <span>
                      {candidate.image} · 第 {candidate.number} 题
                    </span>
                    <MatchStatusBadge status={candidate.match_status} />
                    {candidate.likely_wrong && (
                      <Badge variant="secondary" className="bg-red-50 text-red-700">
                        疑似做错
                      </Badge>
                    )}
                  </div>
                  <p className="text-sm leading-relaxed text-slate-800">{candidate.summary}</p>
                  {candidate.match_status === 'similar' && selected.has(candidate.id) && (
                    <SimilarMatchChoices
                      candidate={candidate}
                      hasChoice={Object.prototype.hasOwnProperty.call(matchChoices, candidate.id)}
                      choice={matchChoices[candidate.id]}
                      onChoose={(canonicalId) => chooseMatch(candidate.id, canonicalId)}
                    />
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
        <div className="fixed inset-x-0 bottom-14 z-10 mx-auto max-w-md border-t border-slate-200 bg-white p-3">
          <Button
            className="w-full"
            size="lg"
            disabled={
              submitting ||
              unresolvedSimilar ||
              (selected.size === 0 && job.fallback_count === 0)
            }
            onClick={confirm}
          >
            {submitting ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> 确认中…
              </>
            ) : (
              '确认录入'
            )}
          </Button>
        </div>
      </div>
    )
  }

  if (jobId) {
    return (
      <div className="px-4 pb-24 pt-6">
        <Card>
          <CardContent className="space-y-4 p-6 text-center">
            <Loader2 className="mx-auto h-10 w-10 animate-spin text-blue-600" />
            <h2 className="text-lg font-bold text-slate-900">
              {job?.phase === 'importing' ? '正在录入错题本' : '正在识别照片中的题目'}
            </h2>
            {job?.phase === 'importing' && (
              <p className="text-sm text-slate-500">
                已完成 {job.done} / {job.total} 项
              </p>
            )}
            {pollFailures >= 3 && (
              <div className="space-y-3 text-sm text-amber-700">
                <p>网络连接不稳定，任务仍在服务端保留。</p>
                <Button variant="outline" size="sm" onClick={reset}>
                  放弃本地任务
                </Button>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    )
  }

  return (
    <div className="px-4 pb-24 pt-6">
      <header className="mb-4">
        <h1 className="text-xl font-bold text-slate-900">录错题</h1>
        <p className="text-sm text-slate-500">拍照上传，AI 自动完成考点分析与归档</p>
      </header>

      <input
        ref={fileRef}
        type="file"
        accept="image/jpeg,image/png,image/webp"
        capture="environment"
        multiple
        className="hidden"
        onChange={(e) => {
          pick(Array.from(e.target.files ?? []))
          e.target.value = ''
        }}
      />

      {!previews.length ? (
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          className="flex h-52 w-full flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed border-slate-300 bg-white text-slate-500 active:bg-slate-50"
        >
          <Camera className="h-10 w-10 text-blue-600" />
          <span className="text-sm font-medium">拍照 / 从相册选择</span>
          <span className="text-xs text-slate-400">JPG · PNG · WEBP，可多选，每张不超过 10MB</span>
        </button>
      ) : (
        <Card>
          <CardContent className="space-y-4 p-4">
            <div className="relative">
              <div className="grid grid-cols-2 gap-2">
                {previews.map((preview, index) => (
                  <img
                    key={preview}
                    src={preview}
                    alt={`错题预览 ${index + 1}`}
                    className="h-36 w-full rounded-lg bg-slate-100 object-contain"
                  />
                ))}
              </div>
              <Button
                variant="secondary"
                size="icon"
                aria-label="清除照片"
                className="absolute right-2 top-2 h-8 w-8 rounded-full"
                onClick={() => pick([])}
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="tags">标签（可选，逗号分隔）</Label>
              <Input
                id="tags"
                value={tags}
                onChange={(e) => setTags(e.target.value)}
                placeholder="例如：期末复习, 几何"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="hint">给老师的话（可选）</Label>
              <Textarea
                id="hint"
                value={hint}
                onChange={(e) => setHint(e.target.value)}
                placeholder="例如：这道大题里只有第 2 小题错了"
                rows={2}
              />
            </div>
            <Button className="w-full" size="lg" disabled={submitting} onClick={submit}>
              {submitting ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" /> 提交中…
                </>
              ) : (
                '开始解析'
              )}
            </Button>
          </CardContent>
        </Card>
      )}

      <button
        type="button"
        onClick={() => setMode('batch')}
        className="mt-6 flex w-full items-center justify-center gap-1.5 text-sm text-slate-400 active:text-blue-600"
      >
        <FileStack className="h-4 w-4" />
        拍的是一整张试卷？试试整卷导入 →
      </button>
    </div>
  )
}

function MatchStatusBadge({ status }: { status: PhotoCandidate['match_status'] }) {
  if (status === 'exact') {
    return <Badge className="bg-blue-50 text-blue-700">精确复用</Badge>
  }
  if (status === 'similar') {
    return <Badge className="bg-amber-50 text-amber-700">相似待确认</Badge>
  }
  return <Badge className="bg-teal-50 text-teal-700">新题解析</Badge>
}

function SimilarMatchChoices({
  candidate,
  hasChoice,
  choice,
  onChoose,
}: {
  candidate: PhotoCandidate
  hasChoice: boolean
  choice: number | null | undefined
  onChoose: (canonicalId: number | null) => void
}) {
  return (
    <div className="space-y-2 rounded-lg bg-amber-50/60 p-2.5">
      <p className="text-xs text-amber-700">请核对候选；未选择前不会复用或解析。</p>
      {candidate.match_candidates.map(({ question, score }) => (
        <div key={question.id} className="space-y-2 rounded-md bg-white p-2.5">
          <MathText text={question.stem_markdown} className="text-xs text-slate-800" />
          <p className="text-xs text-slate-500">
            {question.origin === 'teacher'
              ? '教师发布'
              : question.origin === 'student'
                ? '学生机器校验'
                : '预置题库'}{' '}
            · 相似度 {Math.round(score * 100)}%
          </p>
          {question.analysis_markdown && (
            <p className="line-clamp-2 text-xs text-slate-500">
              解析摘要：{question.analysis_markdown}
            </p>
          )}
          <Button
            type="button"
            size="sm"
            variant={hasChoice && choice === question.id ? 'default' : 'outline'}
            className="w-full"
            aria-pressed={hasChoice && choice === question.id}
            onClick={() => onChoose(question.id)}
          >
            引用此题
          </Button>
        </div>
      ))}
      <Button
        type="button"
        size="sm"
        variant={hasChoice && choice === null ? 'default' : 'outline'}
        className="w-full"
        aria-pressed={hasChoice && choice === null}
        onClick={() => onChoose(null)}
      >
        作为新题解析
      </Button>
    </div>
  )
}
