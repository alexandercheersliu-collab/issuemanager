/** 同类题检测面板：嵌入错题详情，发起检测 → 作答 → 判分 + 降级/回升联动。 */
import { useState } from 'react'
import { CheckCircle2, Loader2, Target, XCircle } from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { api, ApiError } from '@/lib/api'
import { splitStem, type DetectionAnswerResult, type DetectionStart } from '@/types'
import MathText from '@/components/MathText'

type Stage =
  | { name: 'idle' }
  | { name: 'starting' }
  | { name: 'answering'; detection: DetectionStart }
  | { name: 'submitting'; detection: DetectionStart }
  | { name: 'result'; detection: DetectionStart; outcome: DetectionAnswerResult }

export default function DetectionPanel({ questionId }: { questionId: number }) {
  const [stage, setStage] = useState<Stage>({ name: 'idle' })
  const [answer, setAnswer] = useState('')

  async function start() {
    setStage({ name: 'starting' })
    setAnswer('')
    try {
      const detection = await api.startDetection(questionId)
      setStage({ name: 'answering', detection })
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '出题失败，请稍后再试')
      setStage({ name: 'idle' })
    }
  }

  async function submit(detection: DetectionStart) {
    if (!answer.trim()) return
    setStage({ name: 'submitting', detection })
    try {
      const outcome = await api.answerDetection(detection.log_id, answer.trim())
      setStage({ name: 'result', detection, outcome })
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '判分失败，请重试')
      setStage({ name: 'answering', detection })
    }
  }

  if (stage.name === 'idle') {
    return (
      <Button variant="outline" className="w-full" onClick={start}>
        <Target className="mr-2 h-4 w-4" /> 同类题检测
      </Button>
    )
  }

  if (stage.name === 'starting') {
    return (
      <div className="flex items-center justify-center gap-2 py-4 text-sm text-slate-500">
        <Loader2 className="h-4 w-4 animate-spin" /> 正在出题…
      </div>
    )
  }

  const { detection } = stage
  const stem = splitStem(detection.tested.content).stem

  return (
    <div className="space-y-3 rounded-lg border border-blue-100 bg-blue-50/50 p-3">
      <div className="flex items-center gap-1.5">
        <Badge variant="secondary" className="bg-blue-100 text-blue-700">
          同类题检测
        </Badge>
        <Badge variant="outline" className="text-slate-500">
          {detection.tested.difficulty}
        </Badge>
      </div>
      <MathText text={stem} className="text-sm text-slate-800" />

      {(stage.name === 'answering' || stage.name === 'submitting') && (
        <>
          <Textarea
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            placeholder="写下你的答案…"
            rows={2}
            disabled={stage.name === 'submitting'}
          />
          <Button
            className="w-full"
            disabled={!answer.trim() || stage.name === 'submitting'}
            onClick={() => submit(detection)}
          >
            {stage.name === 'submitting' ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> 判分中…
              </>
            ) : (
              '提交答案'
            )}
          </Button>
        </>
      )}

      {stage.name === 'result' && (
        <div className="space-y-2 border-t border-blue-100 pt-3">
          <div className="flex items-center gap-2">
            {stage.outcome.result === 'correct' ? (
              <>
                <CheckCircle2 className="h-5 w-5 text-green-600" />
                <span className="font-semibold text-green-700">答对了</span>
              </>
            ) : (
              <>
                <XCircle className="h-5 w-5 text-red-500" />
                <span className="font-semibold text-red-600">答错了</span>
              </>
            )}
            <span className="text-xs text-slate-500">
              连续通过 {stage.outcome.streak}/{stage.outcome.threshold}
            </span>
          </div>
          <div className="flex flex-wrap items-baseline gap-1 text-sm text-slate-600">
            <span>参考答案：</span>
            <MathText text={stage.outcome.expected_answer} />
          </div>
          <div className="flex flex-wrap gap-1.5">
            {stage.outcome.mastered && (
              <Badge className="bg-green-600">🎉 已掌握，移出复习队列</Badge>
            )}
            {stage.outcome.promoted && !stage.outcome.mastered && (
              <Badge className="bg-teal-600">状态回升，复习间隔延长</Badge>
            )}
            {stage.outcome.demoted && (
              <Badge variant="secondary" className="bg-amber-100 text-amber-700">
                已降级，会更频繁出现
              </Badge>
            )}
          </div>
          <Button variant="outline" size="sm" className="w-full" onClick={start}>
            再来一题
          </Button>
        </div>
      )}
    </div>
  )
}
