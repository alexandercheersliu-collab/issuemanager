import { useCallback, useEffect, useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight, ImageIcon, PartyPopper } from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { Skeleton } from '@/components/ui/skeleton'
import { api, ApiError } from '@/lib/api'
import { splitStem, type Question } from '@/types'
import DetectionPanel from '@/components/DetectionPanel'
import MathText from '@/components/MathText'

const GRADES = [
  { key: 'again', label: '😵 忘了', className: 'bg-red-500 hover:bg-red-600' },
  { key: 'hard', label: '😅 勉强', className: 'bg-orange-500 hover:bg-orange-600' },
  { key: 'good', label: '🙂 记得', className: 'bg-blue-600 hover:bg-blue-700' },
  { key: 'easy', label: '😎 秒懂', className: 'bg-green-600 hover:bg-green-700' },
] as const

function QuestionImage({ questionId }: { questionId: number }) {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    let revoke: string | null = null
    api.imageUrl(questionId).then((u) => {
      revoke = u
      setUrl(u)
    })
    return () => {
      if (revoke) URL.revokeObjectURL(revoke)
    }
  }, [questionId])
  if (!url) return <Skeleton className="h-40 w-full" />
  return (
    <img
      src={url}
      alt="错题原图"
      className="mx-auto max-h-[38vh] w-auto max-w-full rounded-lg object-contain"
    />
  )
}

export default function Review() {
  const [due, setDue] = useState<Question[] | null>(null)
  const [cursor, setCursor] = useState(0)
  const [revealed, setRevealed] = useState(false)
  const [showImage, setShowImage] = useState(false)
  const [gradedCount, setGradedCount] = useState(0)
  const [grading, setGrading] = useState(false)
  // 评「忘了/勉强」后拦截跳题，先引导同类题检测巩固
  const [detectionFor, setDetectionFor] = useState<Question | null>(null)

  const load = useCallback(async () => {
    try {
      const items = await api.due()
      setDue(items)
      setCursor(0)
      setRevealed(false)
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '加载失败')
      setDue([])
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const question = due && due.length > 0 ? due[Math.min(cursor, due.length - 1)] : null
  const { stem, analysis } = useMemo(
    () => (question ? splitStem(question.content_markdown) : { stem: '', analysis: null }),
    [question],
  )
  const visibleTags = question ? question.tags.slice(0, 5) : []

  const advance = useCallback(async () => {
    if (cursor + 1 >= (due?.length ?? 0)) {
      await load() // 本轮结束，重新拉取（忘了的题会回到队列）
    } else {
      setCursor(cursor + 1)
      setRevealed(false)
      setShowImage(false)
    }
  }, [cursor, due, load])

  async function grade(g: (typeof GRADES)[number]['key']) {
    if (!question) return
    setGrading(true)
    try {
      const updated = await api.grade(question.id, g)
      toast.success(`下次复习：${updated.interval_days < 1 ? '稍后' : `${updated.interval_days.toFixed(0)} 天后`}`)
      setGradedCount((n) => n + 1)
      if (g === 'again' || g === 'hard') {
        setDetectionFor(question) // 不熟的题：先巩固，不急着跳下一题
      } else {
        await advance()
      }
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '评分失败')
    } finally {
      setGrading(false)
    }
  }

  async function skipDetection() {
    setDetectionFor(null)
    await advance()
  }

  if (due === null) {
    return (
      <div className="space-y-4 p-4 pt-8">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  if (!question) {
    return (
      <div className="flex min-h-[70dvh] flex-col items-center justify-center px-6 text-center">
        <PartyPopper className="mb-4 h-12 w-12 text-teal-600" />
        <h2 className="text-xl font-bold text-slate-900">今日复习任务已清空</h2>
        <p className="mt-2 text-sm text-slate-500">
          {gradedCount > 0 ? `本轮已评 ${gradedCount} 题，错题已重新排期，明天见。` : '错题本处于健康状态。'}
        </p>
        <Button variant="outline" className="mt-6" onClick={load}>
          刷新看看
        </Button>
      </div>
    )
  }

  return (
    <div className="px-4 pb-24 pt-6">
      <header className="mb-4">
        <h1 className="text-xl font-bold text-slate-900">今日复习</h1>
        <p className="text-sm text-slate-500">
          {due.length} 道待复习 · 进度 {cursor + 1} / {due.length} · 已评 {gradedCount} 题
        </p>
        <Progress value={(cursor / due.length) * 100} className="mt-2 h-1.5" />
      </header>

      {/* 评「忘了/勉强」后的巩固间页：同类题检测，可跳过 */}
      {detectionFor ? (
        <div className="space-y-4">
          <Card className="border-orange-200 bg-orange-50/50">
            <CardContent className="space-y-3 p-4">
              <h2 className="text-base font-semibold text-slate-900">先巩固一下再走 💪</h2>
              <p className="text-sm text-slate-600">
                这道题还不太熟，系统出一道同类题，趁热打铁验证一下？
              </p>
              <DetectionPanel key={detectionFor.id} questionId={detectionFor.id} />
            </CardContent>
          </Card>
          <Button variant="ghost" className="w-full text-slate-500" onClick={skipDetection}>
            不用了，继续下一题 →
          </Button>
        </div>
      ) : (
        <>
      <div className="mb-3 flex flex-wrap gap-1.5">
        <Badge variant="secondary" className="bg-blue-50 text-blue-700">
          {question.difficulty}
        </Badge>
        {visibleTags.map((t) => (
          <Badge key={t} variant="outline" className="text-slate-600">
            {t}
          </Badge>
        ))}
        {question.tags.length > 5 && <Badge variant="outline">+{question.tags.length - 5}</Badge>}
      </div>

      <Card>
        <CardContent className="space-y-4 p-4">
          {question.last_reviewed_at && (
            <p className="text-xs text-slate-400">已连续记牢 {question.reps} 次</p>
          )}

          {/* 题面：文字优先（Markdown + 公式渲染），原图折叠 */}
          {stem ? (
            <MathText text={stem} className="text-[1.05rem] text-slate-800" />
          ) : (
            <QuestionImage questionId={question.id} />
          )}
          {question.image_path && stem && (
            <div>
              <Button
                variant="ghost"
                size="sm"
                className="text-slate-500"
                onClick={() => setShowImage((v) => !v)}
              >
                <ImageIcon className="mr-1.5 h-4 w-4" />
                {showImage ? '收起原图' : '查看原图'}
              </Button>
              {showImage && <QuestionImage questionId={question.id} />}
            </div>
          )}

          {!revealed ? (
            <Button className="w-full" size="lg" onClick={() => setRevealed(true)}>
              显示解析
            </Button>
          ) : (
            <div className="space-y-4 border-t pt-4">
              {analysis && (
                <MathText text={analysis} className="text-[0.95rem] text-slate-700" />
              )}
              <div className="flex flex-wrap items-baseline gap-1 text-sm font-semibold text-slate-900">
                <span>答案：</span>
                <MathText text={question.answer} />
              </div>
              <p className="text-sm font-medium text-slate-500">这道题你掌握得如何？</p>
              <div className="grid grid-cols-2 gap-2.5">
                {GRADES.map((g) => (
                  <Button
                    key={g.key}
                    size="lg"
                    disabled={grading}
                    className={`${g.className} text-white`}
                    onClick={() => grade(g.key)}
                  >
                    {g.label}
                  </Button>
                ))}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <div className="mt-3 flex justify-between">
        <Button
          variant="ghost"
          size="sm"
          disabled={cursor === 0}
          onClick={() => {
            setCursor(cursor - 1)
            setRevealed(false)
            setShowImage(false)
          }}
        >
          <ChevronLeft className="mr-1 h-4 w-4" /> 上一道
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            setCursor((cursor + 1) % due.length)
            setRevealed(false)
            setShowImage(false)
          }}
        >
          先跳过 <ChevronRight className="ml-1 h-4 w-4" />
        </Button>
      </div>
        </>
      )}
    </div>
  )
}
