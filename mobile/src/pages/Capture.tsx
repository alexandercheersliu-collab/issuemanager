import { useRef, useState } from 'react'
import { Camera, CheckCircle2, FileStack, Loader2, X } from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { api, ApiError } from '@/lib/api'
import type { AnalyzeResult } from '@/types'
import MathText from '@/components/MathText'
import BatchImport, { peekPendingJob } from '@/pages/BatchImport'

export default function Capture({ onDone }: { onDone: () => void }) {
  const fileRef = useRef<HTMLInputElement>(null)
  // 有未完成的整卷导入任务时直接恢复到批量流程（解析在服务端跑，回来继续确认入库）
  const [mode, setMode] = useState<'single' | 'batch'>(() =>
    peekPendingJob() ? 'batch' : 'single',
  )
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<string | null>(null)
  const [tags, setTags] = useState('')
  const [hint, setHint] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [result, setResult] = useState<AnalyzeResult | null>(null)

  function pick(f: File | null) {
    setFile(f)
    setResult(null)
    if (preview) URL.revokeObjectURL(preview)
    setPreview(f ? URL.createObjectURL(f) : null)
  }

  async function submit() {
    if (!file) return
    setSubmitting(true)
    try {
      const res = await api.analyze(file, { tags: tags.trim(), hint: hint.trim() })
      setResult(res)
      toast.success('解析完成，已归档到错题本')
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '解析失败，请重试')
    } finally {
      setSubmitting(false)
    }
  }

  function reset() {
    pick(null)
    setTags('')
    setHint('')
    setResult(null)
  }

  // 整卷导入模式（低频功能，入口在单题页底部，不占主导航）
  if (mode === 'batch') {
    return <BatchImport onBack={() => setMode('single')} onDone={onDone} />
  }

  if (result) {
    const incomplete = result.analysis.is_complete === false
    const focused = (result.analysis.focused_sub_question ?? '').trim()
    return (
      <div className="px-4 pb-24 pt-6">
        <Card>
          <CardContent className="space-y-4 p-5 text-center">
            <CheckCircle2 className="mx-auto h-12 w-12 text-green-600" />
            <h2 className="text-lg font-bold text-slate-900">已归档</h2>
            {focused && (
              <p className="rounded-lg bg-teal-50 p-2.5 text-left text-sm text-teal-700">
                已按做错的小题录入：{focused.slice(0, 60)}
                {focused.length > 60 && '…'}
              </p>
            )}
            {incomplete && (
              <p className="rounded-lg bg-amber-50 p-2.5 text-left text-sm text-amber-700">
                题目可能没拍全{result.analysis.completeness_note ? `：${result.analysis.completeness_note}` : ''}，建议重新拍一张完整的
              </p>
            )}
            <div className="flex flex-wrap justify-center gap-1.5">
              {(result.analysis.knowledge_points ?? []).slice(0, 6).map((kp) => (
                <Badge key={kp} variant="secondary" className="bg-teal-50 text-teal-700">
                  {kp}
                </Badge>
              ))}
            </div>
            {result.question.answer && (
              <div className="flex flex-wrap items-baseline justify-center gap-1 text-sm text-slate-600">
                <span>答案：</span>
                <MathText text={result.question.answer} />
              </div>
            )}
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
        className="hidden"
        onChange={(e) => pick(e.target.files?.[0] ?? null)}
      />

      {!preview ? (
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          className="flex h-52 w-full flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed border-slate-300 bg-white text-slate-500 active:bg-slate-50"
        >
          <Camera className="h-10 w-10 text-blue-600" />
          <span className="text-sm font-medium">拍照 / 从相册选择</span>
          <span className="text-xs text-slate-400">JPG · PNG · WEBP，不超过 10MB</span>
        </button>
      ) : (
        <Card>
          <CardContent className="space-y-4 p-4">
            <div className="relative">
              <img src={preview} alt="错题预览" className="max-h-[36vh] w-full rounded-lg object-contain" />
              <Button
                variant="secondary"
                size="icon"
                className="absolute right-2 top-2 h-8 w-8 rounded-full"
                onClick={() => pick(null)}
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
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" /> AI 解析中…
                </>
              ) : (
                '开始解析'
              )}
            </Button>
          </CardContent>
        </Card>
      )}

      {/* 低频入口：整卷导入（多图→PDF→批量入库），不抢单题拍照的主视觉 */}
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
