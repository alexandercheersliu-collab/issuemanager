/** 整卷导入：PDF/DOCX/逐页图片 → 异步切题解构（轮询）→ 校对 → 确认入库。 */
import { useCallback, useEffect, useRef, useState, type SyntheticEvent } from 'react'
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  FileStack,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { api, ApiError } from '@/lib/api'
import type { ConfirmResult, Segment, SegmentsResult } from '@/types'
import AuthImage from '@/components/AuthImage'
import MathText from '@/components/MathText'

type Stage = 'pick' | 'working' | 'review' | 'done'

const POLL_MS = 2000
const MAX_PAGES = 20
const DOCUMENT_SUFFIXES = ['.pdf', '.docx']
const IMAGE_SUFFIXES = ['.jpg', '.jpeg', '.png', '.webp']

function hasSuffix(file: File, suffixes: string[]): boolean {
  const name = file.name.toLowerCase()
  return suffixes.some((suffix) => name.endsWith(suffix))
}

function isDocument(file: File): boolean {
  return hasSuffix(file, DOCUMENT_SUFFIXES)
}

function isImage(file: File): boolean {
  return hasSuffix(file, IMAGE_SUFFIXES)
}

/** 进行中的整卷导入任务号：解析在服务端跑，前端切走/关页面不丢。
 *  持久化到 localStorage，回来时按任务状态恢复到进度页或校对页。 */
const JOB_KEY = 'mm_batch_job'

// eslint-disable-next-line react-refresh/only-export-components
export function peekPendingJob(): string | null {
  return localStorage.getItem(JOB_KEY)
}

export default function BatchImport({ onBack, onDone }: { onBack: () => void; onDone: () => void }) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<File[]>([])
  const [previews, setPreviews] = useState<string[]>([])
  const [stage, setStage] = useState<Stage>('pick')
  const [jobId, setJobId] = useState<string | null>(null)
  const [job, setJob] = useState<SegmentsResult | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [result, setResult] = useState<ConfirmResult | null>(null)
  const [restoreError, setRestoreError] = useState<string | null>(null)

  // 恢复上次未完成的任务：running/pending → 进度页续跑；success 未确认 → 直接进校对页
  const restoreJob = useCallback(async (saved: string) => {
    setRestoreError(null)
    try {
      const s = await api.getDocumentSegments(saved)
      if (s.status === 'success' && !s.confirmed) {
        setJobId(saved)
        setJob(s)
        setStage('review')
        toast.info('上次有一份试卷已解析完，还没确认入库')
      } else if (s.status === 'pending' || s.status === 'running') {
        setJobId(saved)
        setJob(s)
        setStage('working')
        toast.info('已恢复上次未完成的整卷解析')
      } else if (s.status === 'failed' || (s.status === 'success' && s.confirmed)) {
        localStorage.removeItem(JOB_KEY)
      } else {
        setRestoreError(saved)
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        localStorage.removeItem(JOB_KEY)
      } else {
        setRestoreError(saved)
        toast.error('暂时无法恢复上次整卷导入，请重试')
      }
    }
  }, [])

  useEffect(() => {
    const saved = localStorage.getItem(JOB_KEY)
    if (saved) void restoreJob(saved)
  }, [restoreJob])

  // 轮询切题/解构进度，直到 success / failed
  useEffect(() => {
    if (!jobId || stage !== 'working') return
    const timer = setInterval(async () => {
      try {
        const s = await api.getDocumentSegments(jobId)
        setJob(s)
        if (s.status === 'success') {
          clearInterval(timer)
          setStage('review')
        } else if (s.status === 'failed') {
          clearInterval(timer)
          localStorage.removeItem(JOB_KEY)
          toast.error(s.error || '整卷解析失败，请重试')
          setStage('pick')
        }
      } catch {
        /* 单次轮询失败静默，等下一轮 */
      }
    }, POLL_MS)
    return () => clearInterval(timer)
  }, [jobId, stage])

  /** 统一的文件接收入口：同时挂 onChange 与 onInput。
   *
   * 部分手机浏览器（iOS Safari 某些版本、微信/国产内置浏览器）对 file input
   * 只派发 input 或 change 之一；两个事件都挂，进来先读文件再立即清空 value，
   * 天然去重（第二个事件看到空 files 直接忽略），也允许重复选同一批照片。
   */
  function handlePick(e: SyntheticEvent<HTMLInputElement>) {
    const input = e.currentTarget
    const picked = input.files ? Array.from(input.files) : []
    if (!picked.length) return
    input.value = ''
    addFiles(picked)
  }

  function addFiles(list: File[]) {
    try {
      if (list.some((file) => !isDocument(file) && !isImage(file))) {
        toast.error('仅支持 PDF、DOCX、JPEG、PNG、WEBP 文件')
        return
      }
      const next = [...files, ...list]
      if (next.some(isDocument) && next.length > 1) {
        toast.error('PDF/DOCX 请单独上传；多文件上传仅支持试卷图片')
        return
      }
      if (next.length > MAX_PAGES) {
        toast.error(`一次最多导入 ${MAX_PAGES} 页`)
        return
      }
      setFiles(next)
      setPreviews((prev) => [
        ...prev,
        ...list.map((file) => (isImage(file) ? URL.createObjectURL(file) : '')),
      ])
    } catch (err) {
      toast.error(`文件读取失败：${err instanceof Error ? err.message : '请换浏览器重试'}`)
    }
  }

  function removeFile(index: number) {
    if (previews[index]) URL.revokeObjectURL(previews[index])
    setFiles((prev) => prev.filter((_, i) => i !== index))
    setPreviews((prev) => prev.filter((_, i) => i !== index))
  }

  async function submit() {
    if (!files.length) return
    setStage('working')
    setJob(null)
    try {
      const res = await api.importDocument(files)
      setJobId(res.job_id)
      localStorage.setItem(JOB_KEY, res.job_id) // 持久化：切 Tab / 关页面后可恢复
      if (res.duplicated) toast.info('这份文档之前上传过，直接沿用已有任务')
      // 轮询 effect 接管后续状态
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '上传失败，请重试')
      setStage('pick')
    }
  }

  async function removeSegment(index: number) {
    if (!jobId) return
    try {
      await api.deleteSegment(jobId, index)
      setJob(await api.getDocumentSegments(jobId))
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '删除失败')
    }
  }

  async function reanalyzeSegment(index: number) {
    if (!jobId) return
    try {
      await api.reanalyzeSegment(jobId, index)
      setJob(await api.getDocumentSegments(jobId))
    } catch (err) {
      try {
        setJob(await api.getDocumentSegments(jobId))
      } catch {
        /* 保留当前校对状态，原错误仍会提示 */
      }
      toast.error(err instanceof ApiError ? err.message : '重新解析失败')
    }
  }

  async function confirm() {
    if (!jobId) return
    setConfirming(true)
    try {
      const res = await api.confirmImport(jobId)
      localStorage.removeItem(JOB_KEY)
      setResult(res)
      setStage('done')
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '入库失败，请重试')
    } finally {
      setConfirming(false)
    }
  }

  /** 逃生出口：任务卡死（如服务端重启后状态永远 running）时放弃它，回到选页。 */
  function abandon() {
    localStorage.removeItem(JOB_KEY)
    setJobId(null)
    setJob(null)
    setStage('pick')
  }

  // ---------- 完成页 ----------
  if (stage === 'done' && result) {
    return (
      <div className="px-4 pb-24 pt-6">
        <Card>
          <CardContent className="space-y-4 p-5 text-center">
            <CheckCircle2 className="mx-auto h-12 w-12 text-green-600" />
            <h2 className="text-lg font-bold text-slate-900">
              已入库 {result.imported} 道题
            </h2>
            {result.skipped.length > 0 && (
              <div className="space-y-2 text-sm text-amber-700">
                <p>跳过 {result.skipped.length} 道题，未入库</p>
                <details className="rounded-lg bg-amber-50 p-3 text-left">
                  <summary className="cursor-pointer font-medium">
                    查看跳过原因（{result.skipped.length}）
                  </summary>
                  <div className="mt-2 space-y-2">
                    {result.skipped.map((item, index) => (
                      <div key={`${item.number ?? 'unknown'}-${index}`}>
                        <p className="font-medium">第 {item.number ?? '未编号'} 题</p>
                        <p className="text-xs">{item.reason}</p>
                      </div>
                    ))}
                  </div>
                </details>
              </div>
            )}
            <Button className="w-full" onClick={onDone}>
              去错题本看看
            </Button>
          </CardContent>
        </Card>
      </div>
    )
  }

  // ---------- 校对页 ----------
  if (stage === 'review' && job) {
    const importable = job.segments.filter((s) => s.analysis).length
    return (
      <div className="px-4 pb-32 pt-6">
        <header className="mb-4">
          <h1 className="text-xl font-bold text-slate-900">确认识别结果</h1>
          <p className="text-sm text-slate-500">
            共切出 {job.segments.length} 题，{importable} 题可入库
            {job.needs_review_count > 0 && `，${job.needs_review_count} 题建议复核`}
          </p>
        </header>

        <div className="space-y-3">
          {job.segments.map((seg, i) => (
            <SegmentCard
              key={i}
              jobId={jobId!}
              index={i}
              segment={seg}
              onRemove={() => removeSegment(i)}
              onReanalyze={() => reanalyzeSegment(i)}
            />
          ))}
        </div>

        <div className="fixed inset-x-0 bottom-14 z-10 mx-auto max-w-md border-t border-slate-200 bg-white p-3">
          <Button className="w-full" size="lg" disabled={importable === 0 || confirming} onClick={confirm}>
            {confirming ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> 入库中…
              </>
            ) : (
              `确认入库（${importable} 题）`
            )}
          </Button>
        </div>
      </div>
    )
  }

  // ---------- 处理中 ----------
  if (stage === 'working') {
    const total = job?.total ?? 0
    const done = job?.done ?? 0
    return (
      <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 px-4">
        <Loader2 className="h-10 w-10 animate-spin text-blue-600" />
        <p className="text-sm font-medium text-slate-700">
          {!job ? '正在上传文档…' : total > 0 ? `AI 正在逐题解析（${done}/${total}）` : 'AI 正在切分题目…'}
        </p>
        {total > 0 && (
          <div className="h-1.5 w-56 overflow-hidden rounded-full bg-slate-200">
            <div
              className="h-full rounded-full bg-blue-600 transition-all"
              style={{ width: `${Math.round((done / total) * 100)}%` }}
            />
          </div>
        )}
        <p className="text-xs text-slate-400">整卷解析需要一两分钟，可以切出去做别的，回来会接着这里</p>
        <button
          type="button"
          onClick={abandon}
          className="mt-2 text-xs text-slate-400 underline underline-offset-2"
        >
          等太久了？放弃这次导入
        </button>
      </div>
    )
  }

  // ---------- 选择文档或逐页图片 ----------
  return (
    <div className="px-4 pb-24 pt-6">
      <header className="mb-4 flex items-center gap-2">
        <Button variant="ghost" size="icon" onClick={onBack} aria-label="返回">
          <ArrowLeft className="h-5 w-5" />
        </Button>
        <div>
          <h1 className="text-xl font-bold text-slate-900">整卷导入</h1>
          <p className="text-sm text-slate-500">上传 PDF/DOCX，或按页序选择试卷照片</p>
        </div>
      </header>

      {restoreError && (
        <Card className="mb-4 border-amber-200 bg-amber-50/40">
          <CardContent className="space-y-3 p-4">
            <p className="text-sm text-amber-700">上次任务暂时无法恢复，任务仍保留在服务端。</p>
            <Button variant="outline" className="w-full" onClick={() => void restoreJob(restoreError)}>
              重试恢复任务
            </Button>
          </CardContent>
        </Card>
      )}

      <input
        ref={fileRef}
        type="file"
        accept=".pdf,.docx,image/jpeg,image/png,image/webp"
        multiple
        className="hidden"
        onChange={handlePick}
        onInput={handlePick}
      />

      {previews.length === 0 ? (
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          className="flex h-52 w-full flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed border-slate-300 bg-white text-slate-500 active:bg-slate-50"
        >
          <FileStack className="h-10 w-10 text-blue-600" />
          <span className="text-sm font-medium">选择文档 / 试卷照片</span>
          <span className="text-xs text-slate-400">
            一份 PDF/DOCX，或最多 {MAX_PAGES} 张 JPEG/PNG/WEBP
          </span>
        </button>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2">
            {files.map((file, i) => {
              const src = previews[i]
              return (
                <div key={`${file.name}-${i}`} className={`relative ${src ? '' : 'col-span-3'}`}>
                  {src ? (
                    <img src={src} alt={`第 ${i + 1} 页`} className="h-28 w-full rounded-lg object-cover" />
                  ) : (
                    <div className="flex h-28 items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-4">
                      <FileStack className="h-6 w-6 shrink-0 text-blue-600" />
                      <span className="break-all text-sm text-slate-700">{file.name}</span>
                    </div>
                  )}
                  {src && (
                    <span className="absolute left-1 top-1 rounded bg-black/60 px-1.5 text-xs text-white">
                      {i + 1}
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => removeFile(i)}
                    className="absolute right-1 top-1 rounded-full bg-black/60 p-1 text-white"
                    aria-label={src ? `删除第 ${i + 1} 页` : `删除 ${file.name}`}
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              )
            })}
            {!files.some(isDocument) && (
              <button
                type="button"
                onClick={() => fileRef.current?.click()}
                className="flex h-28 flex-col items-center justify-center gap-1 rounded-lg border-2 border-dashed border-slate-300 text-slate-400"
              >
                <Plus className="h-6 w-6" />
                <span className="text-xs">加一页</span>
              </button>
            )}
          </div>
          <Button className="mt-4 w-full" size="lg" onClick={submit}>
            开始识别（{files.length} {isDocument(files[0]) ? '个文档' : '页'}）
          </Button>
        </>
      )}
    </div>
  )
}

function SegmentCard({
  jobId,
  index,
  segment,
  onRemove,
  onReanalyze,
}: {
  jobId: string
  index: number
  segment: Segment
  onRemove: () => void
  onReanalyze: () => void
}) {
  const failed = !segment.analysis || segment.error
  const figurePaths = segment.figure_paths ?? []
  const figureSpecs = segment.figure_specs ?? []
  const page = segment.page_start === segment.page_end
    ? String(segment.page_start)
    : `${segment.page_start}-${segment.page_end}`
  const source = segment.source === 'rule'
    ? '规则切题'
    : segment.source === 'visual'
      ? '视觉切题'
      : segment.source
  return (
    <Card className={failed ? 'border-amber-200 bg-amber-50/40' : undefined}>
      <CardContent className="flex items-start gap-3 p-3">
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-sm font-semibold text-slate-800">
              {segment.number ? `第 ${segment.number} 题` : '未编号'}
            </span>
            {segment.needs_review && (
              <Badge variant="secondary" className="bg-amber-100 text-amber-700">
                <AlertTriangle className="mr-1 h-3 w-3" /> 建议复核
              </Badge>
            )}
            {failed && (
              <Badge variant="secondary" className="bg-red-100 text-red-700">
                解析失败
              </Badge>
            )}
            {segment.analysis?.knowledge_points?.slice(0, 3).map((kp) => (
              <Badge key={kp} variant="secondary" className="bg-teal-50 text-teal-700">
                {kp}
              </Badge>
            ))}
          </div>
          <p className="text-xs text-slate-400">
            <span>第 {page} 页</span>
            <span aria-hidden="true"> · </span>
            <span>{source}</span>
          </p>
          {figurePaths.map((_, figureIndex) => (
            <div key={figureIndex} className="space-y-1">
              <AuthImage
                src={`/documents/${jobId}/segments/${index}/figures/${figureIndex}`}
                alt={`整卷题图 ${figureIndex + 1}`}
                fallback={
                  <AuthImage
                    src={`/documents/${jobId}/pages/${segment.page_start}`}
                    alt={`第 ${segment.page_start} 页原图`}
                    fallback={<p className="text-xs text-amber-700">图形暂不可用</p>}
                  />
                }
              />
              {figureSpecs[figureIndex]?.validation_status === 'fallback' && (
                <p className="text-xs text-amber-700">图形为原图裁剪，请核对准确性</p>
              )}
            </div>
          ))}
          {figureSpecs.length > figurePaths.length && (
            <div className="space-y-1">
              <AuthImage
                src={`/documents/${jobId}/pages/${segment.page_start}`}
                alt={`第 ${segment.page_start} 页原图`}
                fallback={<p className="text-xs text-amber-700">图形暂不可用</p>}
              />
              {figureSpecs
                .slice(figurePaths.length)
                .some((spec) => spec.validation_status === 'fallback') && (
                <p className="text-xs text-amber-700">图形为原图裁剪，请核对准确性</p>
              )}
            </div>
          )}
          <div className="line-clamp-2">
            <MathText text={segment.text} className="text-sm text-slate-600" />
          </div>
          {segment.analysis && segment.error && (
            <p className="text-xs font-medium text-amber-700">
              重新解析失败，将使用上次成功结果
            </p>
          )}
          {segment.error && <p className="text-xs text-red-500">{segment.error}</p>}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={onReanalyze}
            className="flex items-center gap-1 rounded-md px-2 py-1.5 text-xs text-blue-600 active:bg-blue-50"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            重新解析
          </button>
          <button
            type="button"
            onClick={onRemove}
            className="rounded-full p-1.5 text-slate-400 active:bg-slate-100"
            aria-label="删除这题"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      </CardContent>
    </Card>
  )
}
