/** 整卷导入：多图合成 PDF → 异步切题解构（轮询）→ 校对（可删题）→ 确认入库。 */
import { useEffect, useRef, useState, type SyntheticEvent } from 'react'
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  FileStack,
  Loader2,
  Plus,
  Trash2,
  X,
} from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { api, ApiError } from '@/lib/api'
import { imagesToPdf } from '@/lib/pdf'
import type { ConfirmResult, Segment, SegmentsResult } from '@/types'
import MathText from '@/components/MathText'

type Stage = 'pick' | 'working' | 'review' | 'done'

const POLL_MS = 2000
const MAX_PAGES = 20

/** 进行中的整卷导入任务号：解析在服务端跑，前端切走/关页面不丢。
 *  持久化到 localStorage，回来时按任务状态恢复到进度页或校对页。 */
const JOB_KEY = 'mm_batch_job'

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

  // 恢复上次未完成的任务：running/pending → 进度页续跑；success 未确认 → 直接进校对页
  useEffect(() => {
    const saved = localStorage.getItem(JOB_KEY)
    if (!saved) return
    api
      .getDocumentSegments(saved)
      .then((s) => {
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
        } else {
          localStorage.removeItem(JOB_KEY) // failed / 已确认 / 其它终态
        }
      })
      .catch(() => localStorage.removeItem(JOB_KEY)) // 任务不存在（404）等：清掉重来
  }, [])

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
      const next = [...files, ...list]
      if (next.length > MAX_PAGES) {
        toast.error(`一次最多导入 ${MAX_PAGES} 页`)
        return
      }
      setFiles(next)
      setPreviews((prev) => [...prev, ...list.map((f) => URL.createObjectURL(f))])
    } catch (err) {
      toast.error(`照片读取失败：${err instanceof Error ? err.message : '请换浏览器重试'}`)
    }
  }

  function removeFile(index: number) {
    URL.revokeObjectURL(previews[index])
    setFiles((prev) => prev.filter((_, i) => i !== index))
    setPreviews((prev) => prev.filter((_, i) => i !== index))
  }

  async function submit() {
    if (!files.length) return
    setStage('working')
    setJob(null)
    try {
      const pdf = await imagesToPdf(files, 'scan.pdf')
      const res = await api.importDocument(pdf)
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
              <p className="text-sm text-amber-600">
                {result.skipped.length} 道题解析未完成，未入库（可在教师端补录）
              </p>
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
    const importable = job.segments.filter((s) => s.analysis && !s.error).length
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
            <SegmentCard key={i} segment={seg} onRemove={() => removeSegment(i)} />
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
          {!job ? '正在合成并上传文档…' : total > 0 ? `AI 正在逐题解析（${done}/${total}）` : 'AI 正在切分题目…'}
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

  // ---------- 选图页 ----------
  return (
    <div className="px-4 pb-24 pt-6">
      <header className="mb-4 flex items-center gap-2">
        <Button variant="ghost" size="icon" onClick={onBack} aria-label="返回">
          <ArrowLeft className="h-5 w-5" />
        </Button>
        <div>
          <h1 className="text-xl font-bold text-slate-900">整卷导入</h1>
          <p className="text-sm text-slate-500">连拍多张试卷照片，AI 自动切题、逐题解析</p>
        </div>
      </header>

      <input
        ref={fileRef}
        type="file"
        accept="image/*"
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
          <span className="text-sm font-medium">拍照 / 从相册多选</span>
          <span className="text-xs text-slate-400">按页序添加，最多 {MAX_PAGES} 页</span>
        </button>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2">
            {previews.map((src, i) => (
              <div key={i} className="relative">
                <img src={src} alt={`第 ${i + 1} 页`} className="h-28 w-full rounded-lg object-cover" />
                <span className="absolute left-1 top-1 rounded bg-black/60 px-1.5 text-xs text-white">
                  {i + 1}
                </span>
                <button
                  type="button"
                  onClick={() => removeFile(i)}
                  className="absolute right-1 top-1 rounded-full bg-black/60 p-1 text-white"
                  aria-label={`删除第 ${i + 1} 页`}
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              className="flex h-28 flex-col items-center justify-center gap-1 rounded-lg border-2 border-dashed border-slate-300 text-slate-400"
            >
              <Plus className="h-6 w-6" />
              <span className="text-xs">加一页</span>
            </button>
          </div>
          <Button className="mt-4 w-full" size="lg" onClick={submit}>
            开始识别（{files.length} 页）
          </Button>
        </>
      )}
    </div>
  )
}

function SegmentCard({ segment, onRemove }: { segment: Segment; onRemove: () => void }) {
  const failed = !segment.analysis || segment.error
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
          <div className="line-clamp-2">
            <MathText text={segment.text} className="text-sm text-slate-600" />
          </div>
          {segment.error && <p className="text-xs text-red-500">{segment.error}</p>}
        </div>
        <button
          type="button"
          onClick={onRemove}
          className="shrink-0 rounded-full p-1.5 text-slate-400 active:bg-slate-100"
          aria-label="删除这题"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </CardContent>
    </Card>
  )
}
